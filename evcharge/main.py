"""EV-Charger WT service: reads the site through the Modbus proxy, reads the
go-e charger, decides, optionally acts, and exposes state to Home Assistant
(REST + optional MQTT discovery) plus a small web UI.

Safety model:
  * control_enabled = false  -> the service only observes and logs decisions.
  * any hardware write goes through ChargingController.hardware_actions().
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import sys
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs

from .controller import (ChargingController, Settings, Decision, MODES, MODE_MANUAL,
                         cheap_window_info)
from .drivers.goe import GoEClient
from .drivers.solaredge import SolarEdgeSite
from .ha import HaClient
from .proxy import ProxyStatusClient, summary as proxy_summary
from .pv_forecast import Plane, PvForecast, factor as pv_factor
from .safety import SwitchCounter
from .session_meter import SessionMeter

LOG = logging.getLogger("evcharge")

HEARTBEAT_S = 1200       # log the current decision at least this often, even unchanged
SITE_STALE_S = 300       # site snapshot unchanged this long => the reading may be stale
# How often the inverter is read. With a vehicle connected, every cycle - the decision
# needs the surplus. With an empty driveway the site cannot inform anything (the charger
# alone decides "no vehicle connected"), so it is read rarely: the inverter is the weak
# device in this chain and every read is a request it has to serve.
SITE_ACTIVE_S = 30.0
SITE_IDLE_S = 300.0
# On failures the interval backs off and doubles from the base: after the 1st failure the
# base stands (30 s connected / 5 min idle), then 60 s, 120 s, 240 s, 480 s, and the 10
# minute cap from the 6th onwards. A gateway that answers wrong needs quiet to drain, not
# a retry storm - 960 failed reads an hour is what kept the last incident alive all night.
SITE_FAIL_BASE_S = 30.0
SITE_FAIL_MAX_S = 600.0


def site_interval_s(connected: bool, failures: int) -> float:
    """Seconds to wait before reading the site again (pure, so it can be tested)."""
    base = SITE_ACTIVE_S if connected else SITE_IDLE_S
    if failures <= 0:
        return base
    step = min(failures, 8) - 1
    return max(base, min(SITE_FAIL_MAX_S, SITE_FAIL_BASE_S * (2 ** step)))


def _shape(reason: str) -> str:
    """The reason with its numbers masked: "pv: surplus # W below minimum (# W, #p)".

    Reasons carry live values, so a steady state used to re-log almost every cycle -
    an idle night wrote ~360 KB/day of lines that differed only in one number. The
    change test compares this masked form: a transition is still logged once with its
    real numbers, while a state that merely keeps updating a number stays quiet until
    the heartbeat. Nothing important is lost: every charger write is logged on its own
    line, and the heartbeat reports the live values.
    """
    return re.sub(r"[-+]?\d+(?:\.\d+)?", "#", reason or "")

DEFAULT_CONFIG = {
    "site": {"host": "127.0.0.1", "port": 1503, "unit": 1, "battery_capacity_kwh": 10.0},
    "charger": {"host": "192.168.178.22", "phase": 1},
    "interval_s": 5,
    "http": {"host": "0.0.0.0", "port": 7080},
    "mqtt": {"enabled": False, "host": "core-mosquitto", "port": 1883,
             "user": "", "password": "", "topic_prefix": "evcharge",
             "discovery_prefix": "homeassistant"},
    "logging": {"level": "INFO",
                "file": "/home/adermake/EV-CHARGER-WT-HA/logs/evcharge.log"},
    "settings": {"mode": "pv", "min_current": 6.0, "max_current": 14.0, "phases": 1,
                 "buffer_soc": 80.0, "priority_soc": 55.0, "control_enabled": False,
                 "cheap_hours": ["00:00-05:00"]},
}


class Service:
    def __init__(self, config: Dict, config_path: Optional[str] = None):
        self.config = config
        self.config_path = config_path      # file the settings came from, for persistence
        self.settings = Settings(**{**asdict(Settings()), **(config.get("settings") or {})})
        self.controller = ChargingController(self.settings)
        sc = config["site"]
        self.site = SolarEdgeSite(sc.get("host", "127.0.0.1"), int(sc.get("port", 1503)),
                                  int(sc.get("unit", 1)),
                                  float(sc.get("battery_capacity_kwh", 10.0)))
        cc = config["charger"]
        self.charger = GoEClient(cc.get("host"), phase=int(cc.get("phase", 1)))
        # Garage PV is read by its own poller and only exists in Home Assistant, so the
        # driver above cannot see it. Info only: evcharge/ha.py guarantees a failure here
        # leaves the last value (with its age) and can never reach the decision.
        gp = config.get("garage_pv") or {}
        self.garage = None
        self._garage_every = max(5.0, float(gp.get("every_s", 60)))
        self._garage_at = 0.0
        # Site read scheduling: when we last read, when we last succeeded, how many
        # failures in a row (drives the backoff), and the last good reading (reused
        # while we deliberately are not reading).
        self._site_at = 0.0
        self._site_ok_at = None
        self._site_failures = 0
        self._site_last = None
        # Modbus proxy health (display only): the link to the inverter is the part that
        # fails silently, because the proxy answers from cache when the device is gone.
        pc = config.get("proxy_status") or {}
        self.proxy_status = None
        self._proxy_every = max(5.0, float(pc.get("every_s", 15)))
        self._proxy_at = 0.0
        # Charge-switching safety (see safety.py): count the on/off transitions we really
        # apply to the wallbox and latch a fault if the control loop starts cycling the
        # car. A car's on-board charger is not a switch.
        sf = config.get("safety") or {}
        self.safety = SwitchCounter(threshold=int(sf.get("threshold", 5)),
                                    window_s=float(sf.get("window_min", 30)) * 60.0)
        if pc.get("enabled", True):
            self.proxy_status = ProxyStatusClient(
                str(pc.get("url") or "http://192.168.178.44:1504/status"),
                timeout=float(pc.get("timeout_s", 3.0)))
        if gp.get("enabled"):
            self.garage = HaClient(str(gp.get("entity") or "sensor.garage_pv_leistung"),
                                   timeout=float(gp.get("timeout_s", 3.0)),
                                   env_file=(config.get("ha") or {}).get("env_file"))
        # Session energy, measured a second time at the SDM630 in the garage (session_meter.py):
        # the wallbox's own figure under-reads, so the owner wants his own number next to it,
        # plus a CSV row for every finished session. Both counters are read on their own
        # cadence - the meter may ask every cycle, the cache decides when Home Assistant is
        # actually bothered.
        sd = config.get("sdm") or {}
        self.session_meter = None
        self._sdm_every = max(5.0, float(sd.get("every_s", 30)))
        self._sdm_at = 0.0
        self._sdm_cache = None
        self._sdm_readers = []
        self._accum_at = 0.0             # for the go-e session integration (measured dt)
        if sd.get("enabled"):
            env_file = (config.get("ha") or {}).get("env_file")
            self._sdm_readers = [
                HaClient(str(sd.get("entity_import") or ""), env_file=env_file,
                         timeout=float(sd.get("timeout_s", 3.0))),
                HaClient(str(sd.get("entity_export") or ""), env_file=env_file,
                         timeout=float(sd.get("timeout_s", 3.0))),
                # The liveness probe, and it must be a value that MOVES on every poll. Home
                # Assistant does not re-write an unchanged state, so a counter (frozen while
                # nothing is drawn) and even the *power* (0.00 W all night) sit untouched for
                # hours and would look like a dead meter. A phase voltage jitters by a few
                # tenths on every read - measured here: updated every ~15 s.
                HaClient(str(sd.get("entity_live") or "sensor.sdm630_l1_spannung"),
                         env_file=env_file, timeout=float(sd.get("timeout_s", 3.0))),
                # The garage PV's own lifetime counter, for the third session figure (the
                # meter's net plus what the inverter fed into the same feeder). Its poller
                # sleeps at night, so an absent value here is normal, never an error.
                HaClient(str(sd.get("entity_pv") or "sensor.garage_pv_energie"),
                         env_file=env_file, timeout=float(sd.get("timeout_s", 3.0))),
                # ...and the inverter's liveness probe, for the same reason the meter needs
                # one: a *counter* is not rewritten while it does not move (measured here:
                # 279.56 kWh sat untouched while the poller read it every 30 s), so its own
                # timestamp is useless as a freshness check. The power reading moves on every
                # poll, so it is the one that can go stale.
                HaClient(str(sd.get("entity_pv_power") or "sensor.garage_pv_leistung"),
                         env_file=env_file, timeout=float(sd.get("timeout_s", 3.0))),
            ]
            self.session_meter = SessionMeter(
                read=self._sdm_counters,
                csv_path=str(sd.get("csv") or "logs/sdm_sessions.csv"),
                unplug_cycles=int(sd.get("unplug_cycles", 2)),
                stale_s=float(sd.get("stale_s", 600)),
                pv_enabled=bool(sd.get("entity_pv", True)))
        # Local PV forecast (pv_forecast.py). Step 1 of the owner's plan: display and log it,
        # steer NOTHING with it - he wants to see whether a forecast is any good on his roof
        # before it may move the priority. So nothing in this block reaches a decision; it
        # integrates the day's measured yields (from the same site reading, over the measured
        # cycle time) and keeps today's CSV row on disk, so a restart costs at most the write
        # interval.
        self._fc_cfg = config.get("forecast") or {}
        self._fc_capacity = float((config.get("site") or {}).get("battery_capacity_kwh", 10.0))
        self.forecast = None
        self._fc_at = 0.0
        self._fc_write_s = max(30.0, float(self._fc_cfg.get("write_s", 300)))
        self._fc_written_at = 0.0
        self._fc_day = ""
        self._fc = self._fc_blank(self._fc_day)
        if self._fc_cfg.get("enabled"):
            settings_cfg = config.get("settings") or {}
            planes = [Plane(float(p.get("kwp", 0.0)), float(p.get("azimuth", 0.0)),
                            float(p.get("tilt", 25.0)))
                      for p in (self._fc_cfg.get("planes") or config.get("planes") or [])]
            self.forecast = PvForecast(
                planes=planes,
                lat=float(self._fc_cfg.get("lat", 0.0)),
                lon=float(self._fc_cfg.get("lon", 0.0)),
                pr=float(self._fc_cfg.get("pr", 0.85)),
                tz=str(self._fc_cfg.get("timezone") or settings_cfg.get("timezone")
                       or "Europe/Berlin"),
                every_s=float(self._fc_cfg.get("every_s", 3600)),
                cache_path=str(self._fc_cfg.get("cache") or "logs/pv_forecast.json"),
                timeout=float(self._fc_cfg.get("timeout_s", 20)))
            self._fc_load_today()
        self.state: Dict = {
            "started_at": time.time(), "control_enabled": self.settings.control_enabled,
            "mode": self.settings.mode, "site": {}, "charger": {}, "decision": {},
            "garage": {},           # garage PV info row (HA value + its age), display only
            "sdm": {},              # second session figure + the last one (SDM630)
            "forecast": {},         # PV forecast + today's measured totals (display only)
            "actions": [], "errors": [], "cycles": 0, "last_cycle": None,
            # Published from the first cycle on, so the UI and the MQTT discovery template
            # never have to cope with the key being absent.
            "session_kwh": 0.0,
            "mqtt_connected": False, "dry_run": not self.settings.control_enabled,
            "manual_since": None,   # epoch when manual mode was entered, else None
        }
        self._stop = asyncio.Event()
        self._lock = threading.Lock()
        self.mqtt = None

    # ---------------- main loop ----------------
    async def run(self) -> None:
        interval = float(self.config.get("interval_s", 5))
        LOG.info("service loop starting, interval %.0fs, control_enabled=%s",
                 interval, self.settings.control_enabled)
        while not self._stop.is_set():
            t0 = time.time()
            await asyncio.get_event_loop().run_in_executor(None, self.cycle)
            wait = max(0.5, interval - (time.time() - t0))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    def _read_warn(self, what: str, exc: Exception) -> None:
        """Warn about a failing device read, at most once per heartbeat.

        A device read can fail for hours - the inverter's Modbus target is off every
        night - while the loop retries every cycle, so warning each time would write
        ~1500 lines a night for one known condition. The current error is always in
        state["errors"] and the freshness badge, so nothing is hidden.
        """
        now = time.time()
        key = "_warned_" + what
        if now - getattr(self, key, 0.0) >= HEARTBEAT_S:
            setattr(self, key, now)
            LOG.warning("%s read failed: %s", what, exc)

    def _sdm_counters(self) -> Optional[Dict]:
        """The SDM630 counters, refreshed on their own cadence and cached in between.

        The session meter asks every cycle; Home Assistant is only bothered every `every_s`,
        because the counters move slowly and HA is not a device this plant should hammer. A
        failed refresh returns None, which the meter records as a reading failure - never as
        energy.
        """
        if self._sdm_cache is not None and (time.time() - self._sdm_at) < self._sdm_every:
            return self._sdm_cache
        self._sdm_at = time.time()
        if not self._sdm_readers:
            return None
        imports = self._sdm_readers[0].get_state()
        exports = self._sdm_readers[1].get_state() if len(self._sdm_readers) > 1 else None
        live = self._sdm_readers[2].get_state() if len(self._sdm_readers) > 2 else None
        pv = self._sdm_readers[3].get_state() if len(self._sdm_readers) > 3 else None
        pv_live = self._sdm_readers[4].get_state() if len(self._sdm_readers) > 4 else None
        if imports is None or imports.get("value") is None:
            return None
        self._sdm_cache = {
            "import_kwh": imports["value"],
            "export_kwh": (exports or {}).get("value"),
            # From the moving value, not from the counter (see the session meter's docstring).
            "age_s": (live or {}).get("age_s"),
            "counter_age_s": imports.get("age_s"),
            # The inverter's counter, for the third session figure. It may be unavailable
            # (its poller sleeps at night) or stale; the session meter decides what that
            # means, this cache only reports what Home Assistant had. The age comes from the
            # *power* entity: the counter is not rewritten while it stands still, so only the
            # moving value can say whether the inverter is still being polled.
            "pv_energy_kwh": (pv or {}).get("value"),
            "pv_age_s": (pv_live or {}).get("age_s"),
        }
        return self._sdm_cache

    def _safety_force_on(self) -> None:
        """The safety action itself: leave the car charging steadily, then stop writing.

        Called once, when the switching counter latches. From here on the app writes
        nothing to the wallbox, so the car sees a stable enable instead of a train of
        on/off edges - which is the whole point: an on-board charger that is switched
        like a relay is an expensive repair.
        """
        try:
            self.charger.set_charging(True)
            LOG.error("SAFETY: charging left enabled on purpose - the app is now read-only "
                      "towards the wallbox. Clear the fault in the web UI when the cause "
                      "is understood")
        except Exception as exc:  # noqa: BLE001
            LOG.error("SAFETY: could not enable charging for the fault state: %s", exc)

    def cycle(self) -> None:
        errors = []
        site_state = None
        charger_state = None
        # The wallbox is read every cycle: it is what says whether a vehicle is there,
        # and that answer decides how often the inverter is read at all.
        try:
            charger_state = self.charger.status()
        except Exception as exc:  # noqa: BLE001
            errors.append("charger: %s" % exc)
            self._read_warn("charger", exc)

        connected = bool(charger_state is not None and charger_state.connected)
        every = site_interval_s(connected, self._site_failures)
        due = (time.time() - self._site_at) >= every or self._site_last is None
        skipped = False
        with self._lock:
            self.state["site_cadence_s"] = every
            self.state["site_failures"] = self._site_failures
            # The cheap window in the house's own clock, so "are these my hours?" is
            # answerable on screen rather than by reading the code.
            self.state["cheap_window"] = cheap_window_info(self.settings)
            # Publish the settings every cycle, not only after a change: the web UI fills
            # its input fields from here, and without this the card shows empty boxes (or
            # placeholder text, which reads as "greyed out") until someone changes
            # something - exactly when the owner wants to check what is in force.
            self.state["settings"] = self.settings.to_dict()
            # Charge-switching safety, shown in the UI: transitions applied in the window.
            self.state["safety"] = self.safety.as_state()
        if due:
            try:
                site_state = self.site.read()
                self._site_at = time.time()
                self._site_ok_at = self._site_at
                self._site_last = site_state
                self._site_failures = 0
            except Exception as exc:  # noqa: BLE001
                # No site reading this cycle means no decision - deliberately: acting on
                # remembered numbers is what the stale gate exists to prevent.
                self._site_failures += 1
                errors.append("site: %s" % exc)
                self._read_warn("site", exc)
        else:
            # Deliberately not reading yet (idle cadence or a backoff after failures).
            # The last good reading stands in, which is safe precisely because in this
            # branch no vehicle is connected: the only decision it can produce is
            # "no vehicle connected".
            site_state = self._site_last
            skipped = True

        # Garage PV info row (display only). Polled at its own, slower cadence: HA holds
        # a value its own poller refreshed minutes ago, so asking every cycle would buy
        # nothing. A failed read keeps the previous value + age rather than blanking it.
        if self.garage is not None and (time.time() - self._garage_at) >= self._garage_every:
            self._garage_at = time.time()
            got = self.garage.get_state()
            with self._lock:
                if got is not None:
                    self.state["garage"] = {"pv_w": got["value"], "age_s": got["age_s"],
                                            "entity": self.garage.entity_id,
                                            "read_at": time.time()}
                elif not self.state.get("garage"):
                    errors.append("garage: no value")

        # Modbus proxy health (display only, own cadence). The proxy answers clients from
        # cache when it cannot reach the inverter, so a frozen meter looks healthy from
        # here - that is exactly how a 657-minute-old reading once went unnoticed. Its
        # /status is the only place that says so, and this card is where it is shown.
        if self.proxy_status is not None and (time.time() - self._proxy_at) >= self._proxy_every:
            self._proxy_at = time.time()
            pstats = self.proxy_status.fetch()
            with self._lock:
                self.state["proxy"] = proxy_summary(
                    pstats, failures=self.proxy_status.failures)
                self.state["proxy"]["read_at"] = time.time()

        # Stale-reading guard. If the site snapshot is identical for minutes, the
        # reading path (app -> proxy -> inverter) is repeating its last frame instead of
        # measuring: the proxy answers from cache when the SolarEdge link fails, and its
        # status endpoint shows thousands of such reconnects. The decision log cannot
        # show this - the reason string simply stops changing - so it gets its own
        # signal, in the log and in the web UI.
        if site_state is not None and not skipped:
            snap = (round(site_state.pv_power_w), round(site_state.grid_power_w),
                    round(site_state.battery_power_w), round(site_state.battery_soc or 0.0))
            if snap != getattr(self, "_site_snap", None):
                self._site_since = None
            elif getattr(self, "_site_since", None) is None:
                self._site_since = time.time()
            self._site_snap = snap
            held = (time.time() - self._site_since) if getattr(self, "_site_since", None) else 0.0
            if held >= SITE_STALE_S and (time.time() - getattr(self, "_stale_warned_at", 0.0)) >= HEARTBEAT_S:
                self._stale_warned_at = time.time()
                LOG.warning("site reading unchanged for %.0f min (%s) - the meter or the "
                            "proxy may be serving a stale frame", held / 60.0, snap)
        elif skipped:
            # We chose not to look, so the snapshot-identity check must not run (nothing
            # changed *because* nothing was read). Honest number: age of the last good
            # reading. It stays well inside the stale gate at the idle cadence.
            held = (time.time() - self._site_ok_at) if self._site_ok_at else -1.0
        else:
            # A failed read is not freshness. Report how long ago the last *good* read
            # was, or the badge would say "meter live" while the numbers on screen are
            # hours old (the inverter is off every night, and that is when it matters).
            # -1 means: no good read since this process started.
            held = (time.time() - self._site_ok_at) if getattr(self, "_site_ok_at", None) else -1.0

        decision: Optional[Decision] = None
        actions = []
        if site_state is not None and charger_state is not None:
            decision = self.controller.decide(site_state, charger_state,
                                              session_kwh=self.state.get("session_kwh", 0.0),
                                              site_stale_s=held)
            actions = self.controller.hardware_actions(decision, charger_state,
                                                       site_stale_s=held)
            if self.safety.fault:
                # Fault latched: the charge has already been turned on once and nothing
                # more may be written to the wallbox. The decision keeps running for the
                # display; it simply cannot touch the hardware any more (see safety.py).
                actions = []
            for a in actions:
                try:
                    if a.startswith("alw="):
                        self.charger.set_charging(a == "alw=1")
                        latched = self.safety.note(a)
                        if latched:
                            LOG.error("SAFETY: %s - keeping the charge enabled and writing "
                                      "nothing more to the wallbox until this is cleared",
                                      latched)
                            self._safety_force_on()
                    elif a.startswith("amx="):
                        self.charger.set_max_current(float(a.split("=")[1]))
                    elif a.startswith("amp=") or a.startswith("frc="):
                        # labels from older builds. They are NOT mapped silently: an
                        # unknown label must be loud, because a silent rename once left
                        # the app believing it controlled the car while it did not.
                        raise ValueError("refusing legacy charger label %r" % a)
                    else:
                        # loud, not silent: a label/parser mismatch here means the app
                        # believes it is controlling the car when it is not
                        raise ValueError("unknown charger action %r" % a)
                    LOG.info("applied charger command %s", a)
                except Exception as exc:  # noqa: BLE001
                    errors.append("action %s: %s" % (a, exc))
                    LOG.error("charger command %s failed: %s", a, exc)

        with self._lock:
            self.state["cycles"] += 1
            self.state["last_cycle"] = time.time()
            self.state["site_unchanged_s"] = round(held, 1)
            self.state["errors"] = errors[-10:]
            self.state["actions"] = actions
            self.state["mode"] = self.settings.mode
            self.state["control_enabled"] = self.settings.control_enabled
            # manual mode: remember since when, so the UI can show how long the app
            # has been out of the way (and a forgotten manual mode is obvious).
            manual = self.settings.mode == MODE_MANUAL
            if manual and self.state["manual_since"] is None:
                self.state["manual_since"] = time.time()
                LOG.info("manual mode ON: the app stops controlling the charger (%d actions suppressed)",
                         0 if decision is None else len(actions))
            elif not manual and self.state["manual_since"] is not None:
                LOG.info("manual mode OFF after %.0f min: the app resumes control",
                         (time.time() - self.state["manual_since"]) / 60.0)
                self.state["manual_since"] = None
            # Log decision transitions - the reason string carries the answer to "did
            # the grace start counting?" ("waiting enable delay 60s", ...). The change
            # test masks the numbers (_shape), so a steady state stops re-logging every
            # cycle just because the surplus moved a watt. Every HEARTBEAT_S a short
            # "hb:" line still reports the live values, so silence in the log can never
            # again be mistaken for an app that is not running (an unplugged car hid
            # eight hours of correct decisions that way).
            now_log = time.time()
            key = _shape(decision.reason) if decision is not None else None
            changed = decision is not None and key != getattr(self, "_last_reason_key", None)
            due = (now_log - getattr(self, "_last_log_at", 0.0)) >= HEARTBEAT_S
            if decision is not None and (changed or due):
                if changed:
                    LOG.info("decision: charge=%s target=%.1fA surplus=%.0fW %s",
                             decision.charge, decision.target_current, decision.surplus_w,
                             decision.reason)
                else:
                    LOG.info("hb: charge=%s target=%.1fA surplus=%.0fW car=%s alw=%s",
                             decision.charge, decision.target_current, decision.surplus_w,
                             charger_state.car_state if charger_state is not None else "-",
                             charger_state.enabled if charger_state is not None else "-")
                self._last_reason = decision.reason
                self._last_reason_key = key
                self._last_log_at = now_log
            self.state["dry_run"] = not self.settings.control_enabled
            if site_state is not None:
                self.state["site"] = {k: v for k, v in asdict(site_state).items() if k != "raw"}
            if charger_state is not None:
                self.state["charger"] = {k: v for k, v in asdict(charger_state).items()
                                         if k != "raw"}
            # The go-e session figure: integrated from the wallbox's own per-phase
            # measurements over the *measured* cycle time. `interval_s` is the configured
            # target, not what the loop actually took, so using it over-counts whenever the
            # loop runs faster than the setting - and this figure is the one the owner
            # compares his own meter against. Reset when the car goes away, so "session"
            # means one plug-in; the SDM meter below freezes its own copy at the same edge.
            now_ts = time.time()
            dt_s = 0.0 if not self._accum_at else max(0.0, min(300.0, now_ts - self._accum_at))
            self._accum_at = now_ts
            if charger_state is not None and charger_state.charging:
                self.state["session_kwh"] = self.state.get("session_kwh", 0.0) + \
                    charger_state.power * (dt_s / 3600.0) / 1000.0
            if not connected:
                self.state["session_kwh"] = 0.0
            if decision is not None:
                self.state["decision"] = asdict(decision)
        # SDM session meter - the second, independent session figure (session_meter.py). Fed
        # after the go-e one so the CSV keeps the session's final value, and deliberately
        # outside the state lock: a slow Home Assistant read must not block the web UI.
        if self.session_meter is not None and charger_state is not None:
            self.session_meter.update(connected=connected,
                                      goe_session_kwh=self.state.get("session_kwh", 0.0))
            with self._lock:
                self.state["sdm"] = self.session_meter.as_state()
        # PV forecast (pv_forecast.py) - display and logging only, step 1 of the plan. Refresh
        # on its own cadence, add this cycle to the day's totals, keep today's row on disk.
        # Nothing in this block can reach a decision; if the internet is gone the module keeps
        # the previous forecast and only flags it stale.
        if self.forecast is not None:
            self.forecast.update()
            fc_ts = time.time()
            fc_dt = 0.0 if not self._fc_at else max(0.0, min(300.0, fc_ts - self._fc_at))
            self._fc_at = fc_ts
            fday = self.forecast.hour_now[:10]
            if fday != self._fc_day:
                if self._fc_day:
                    self._fc_flush(final=True)     # close yesterday, reset for today
                self._fc_reset(fday)
            # Only a *fresh* reading may be integrated: on non-due cycles `site_state` is the
            # cached last one (fine - a zero-order hold), but if the inverter read has been
            # failing, that cached value is old, and adding it would inflate the day's measured
            # figure with energy that was never produced. The stale gate the decisions use
            # applies to the evidence as well.
            fresh = bool(self._site_ok_at) and \
                (fc_ts - self._site_ok_at) <= float(self.settings.site_stale_s)
            self._fc_integrate(site_state if fresh else None, fc_dt,
                               car_w=(charger_state.power if charger_state is not None else 0.0))
            if fc_ts - self._fc_written_at >= self._fc_write_s:
                self._fc_flush(final=False)
                self._fc_written_at = fc_ts
            with self._lock:
                self.state["forecast"] = self.forecast.as_state(
                    measured_today_kwh=self._fc["ac_kwh"],
                    soc=(site_state.battery_soc if site_state is not None else None),
                    soc_target=self.settings.priority_soc,
                    capacity_kwh=self._fc_capacity,
                    house_rest_kwh=float(self._fc_cfg.get("house_reserve_kwh", 3.0)),
                    margin=float(self._fc_cfg.get("margin", 1.3)))
        if self.mqtt is not None:
            try:
                self.mqtt.publish_state(self.state)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("mqtt publish failed: %s", exc)
    # ---------------- PV forecast: the day's evidence (display only) ----------------
    def _fc_blank(self, day: str) -> Dict:
        return {"day": day, "ac_kwh": 0.0, "array_kwh": 0.0, "house_kwh": 0.0,
                "car_kwh": 0.0, "charge_kwh": 0.0, "discharge_kwh": 0.0,
                "soc_min": None, "soc_max": None, "samples": 0}

    def _fc_reset(self, day: str) -> None:
        self._fc_day, self._fc = day, self._fc_blank(day)

    def _fc_integrate(self, site_state, dt_s: float, car_w: float = 0.0) -> None:
        """Add one site reading to the day's totals. Pure bookkeeping - no decision reads it.

        The resolution is whatever the app's own read cadence gives: with a car connected it
        reads the inverter every cycle, with an empty driveway it throttles (that is the
        owner's standing rule: no extra polling load on the inverter), so `samples` is written
        into the row and the measured figure is to be read with that resolution in mind.
        """
        if site_state is None or dt_s <= 0.0:
            return
        d = self._fc
        h = dt_s / 3600.0 / 1000.0                      # W * h -> kWh
        ac = max(0.0, float(getattr(site_state, "inverter_ac_w", 0.0) or 0.0))
        arr = max(0.0, float(getattr(site_state, "pv_power_w", 0.0) or 0.0))
        grid = float(getattr(site_state, "grid_power_w", 0.0) or 0.0)      # + = import
        batt = float(getattr(site_state, "battery_power_w", 0.0) or 0.0)   # + = discharge
        d["ac_kwh"] += ac * h
        d["array_kwh"] += arr * h
        # Energy balance at the AC node: inverter + grid = house + car. The inverter's AC
        # output already carries whatever the battery adds or takes (DC side), so the battery
        # needs no term of its own here. Clamped at 0: a sign slip must not write negative
        # consumption into the file.
        d["house_kwh"] += max(0.0, ac + grid - float(car_w or 0.0)) * h
        d["car_kwh"] += max(0.0, float(car_w or 0.0)) * h
        if batt < 0.0:
            d["charge_kwh"] += -batt * h
        else:
            d["discharge_kwh"] += batt * h
        soc = getattr(site_state, "battery_soc", None)
        if soc is not None:
            soc = float(soc)
            d["soc_min"] = soc if d["soc_min"] is None else min(d["soc_min"], soc)
            d["soc_max"] = soc if d["soc_max"] is None else max(d["soc_max"], soc)
        d["samples"] += 1

    def _fc_row(self, final: bool):
        """One row of evidence: forecast against measured, plus what house and battery did."""
        if self.forecast is None or self.forecast.data is None or not self._fc_day:
            return None
        date = self._fc_day
        upto = (date + "T23:59") if final else self.forecast.hour_now
        d = self._fc
        exp_sofar = self.forecast.data.until_kwh(date, upto)
        exp_sofar_dc = self.forecast.data.until_kwh(date, upto, ac=False)
        return {
            "row": "final" if final else "partial", "date": date,
            "forecast_day_kwh": round(self.forecast.data.day_kwh(date), 3),
            "forecast_sofar_kwh": round(exp_sofar, 3),
            "forecast_rest_kwh": round(self.forecast.data.remaining_kwh(date, upto), 3),
            "measured_ac_kwh": round(d["ac_kwh"], 3),
            "measured_array_kwh": round(d["array_kwh"], 3),
            # AC against the AC estimate, array (DC side) against the DC estimate - the two
            # factors answer different questions and must not be mixed.
            "factor_ac": pv_factor(d["ac_kwh"], exp_sofar),
            "factor_array": pv_factor(d["array_kwh"], exp_sofar_dc),
            "house_kwh": round(d["house_kwh"], 3), "car_kwh": round(d["car_kwh"], 3),
            "battery_charge_kwh": round(d["charge_kwh"], 3),
            "battery_discharge_kwh": round(d["discharge_kwh"], 3),
            "soc_min": d["soc_min"], "soc_max": d["soc_max"], "samples": d["samples"],
            "fetches": self.forecast.fetches, "failures": self.forecast.failures,
        }

    def _fc_flush(self, final: bool) -> None:
        row = self._fc_row(final)
        if row is None:
            return
        # Today's row lives in its own file and is rewritten as it grows: a restart, or a look
        # at the file at noon, never loses the day. Only the finished day is appended to the CSV.
        path = os.path.expanduser(str(self._fc_cfg.get("today") or "logs/pv_forecast_today.json"))
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(row, fh, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except OSError as exc:
            LOG.warning("could not write %s: %s", path, exc)
        if not final:
            return
        csv_path = os.path.expanduser(str(self._fc_cfg.get("csv") or "logs/pv_forecast.csv"))
        try:
            os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
            fresh = not os.path.exists(csv_path)
            with open(csv_path, "a") as fh:
                if fresh:
                    fh.write(",".join(row.keys()) + "\n")
                fh.write(",".join("" if v is None else str(v) for v in row.values()) + "\n")
            LOG.info("PV day closed: forecast %.1f kWh | measured AC %.1f kWh (factor %s) | "
                     "array %.1f kWh | house %.1f kWh | car %.1f kWh | battery +%.1f/-%.1f kWh | "
                     "SOC %s..%s%% | %d samples",
                     row["forecast_day_kwh"], row["measured_ac_kwh"], row["factor_ac"],
                     row["measured_array_kwh"], row["house_kwh"], row["car_kwh"],
                     row["battery_charge_kwh"], row["battery_discharge_kwh"],
                     row["soc_min"], row["soc_max"], row["samples"])
        except OSError as exc:
            LOG.warning("could not append to %s: %s", csv_path, exc)

    def _fc_load_today(self) -> None:
        """Pick today's partial row back up after a restart (a leftover from yesterday is not
        resumed - the new day starts clean)."""
        try:
            path = os.path.expanduser(
                str(self._fc_cfg.get("today") or "logs/pv_forecast_today.json"))
            if not os.path.exists(path):
                return
            with open(path) as fh:
                row = json.load(fh)
            day = self.forecast.hour_now[:10]
            if str(row.get("date")) != day:
                return
            self._fc_day = day
            self._fc = {"day": day, "ac_kwh": float(row.get("measured_ac_kwh") or 0.0),
                        "array_kwh": float(row.get("measured_array_kwh") or 0.0),
                        "house_kwh": float(row.get("house_kwh") or 0.0),
                        "car_kwh": float(row.get("car_kwh") or 0.0),
                        "charge_kwh": float(row.get("battery_charge_kwh") or 0.0),
                        "discharge_kwh": float(row.get("battery_discharge_kwh") or 0.0),
                        "soc_min": row.get("soc_min"), "soc_max": row.get("soc_max"),
                        "samples": int(row.get("samples") or 0)}
            LOG.info("today's PV row resumed from disk: %.2f kWh measured AC, %d samples",
                     self._fc["ac_kwh"], self._fc["samples"])
        except (OSError, ValueError, TypeError) as exc:
            LOG.warning("could not read today's PV row: %s", exc)

    def update_settings(self, patch: Dict) -> Dict:
        allowed = set(asdict(Settings()).keys())
        unknown = [k for k in patch if k not in allowed]
        # Validate BEFORE applying: a rejected patch must leave the settings untouched.
        # (It used to set the value first and complain afterwards, so a bad mode stayed
        # in the settings while the caller was told "no".) An unknown mode is refused
        # loudly rather than quietly reset to "pv" - a silent fallback is how a typo
        # moves the app into a mode nobody asked for.
        if "mode" in patch and patch["mode"] not in MODES:
            return {"ok": False, "error": "unknown mode %r" % patch["mode"],
                    "modes": list(MODES), "settings": self.settings.to_dict()}
        for k, v in patch.items():
            if k not in allowed:
                continue
            cur = getattr(self.settings, k)
            if isinstance(cur, bool):
                v = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "on", "yes")
            elif isinstance(cur, float):
                v = float(v)
            elif isinstance(cur, int) and not isinstance(cur, bool):
                v = int(v)
            elif isinstance(cur, list) and isinstance(v, str):
                v = [x.strip() for x in v.split(",") if x.strip()]
            setattr(self.settings, k, v)
        if self.settings.mode not in MODES:
            self.settings.mode = MODE_PV
        with self._lock:
            self.state["settings"] = self.settings.to_dict()
        LOG.info("settings updated: %s", {k: v for k, v in patch.items() if k in allowed})
        return {"ok": True, "unknown": unknown, "settings": self.settings.to_dict(),
                **self._persist_settings()}

    def _persist_settings(self) -> Dict:
        """Write the current settings back into the config file they were loaded from.

        The file is read once at startup, so without this every change made in the web UI
        or through the API lived only in memory and a restart silently reverted it - which
        is why the page always came back as DRY RUN and why mode/limits reset. The write
        goes through a temp file + os.replace so a crash mid-write cannot leave a
        truncated config that the next start would refuse to read; the previous content
        is kept as <file>.bak.

        The Home Assistant add-on options file (/data/options.json) is deliberately NOT
        written: the supervisor owns that file, and an edit the supervisor does not know
        about would be lost or conflict.
        """
        path = self.config_path
        if not path:
            return {"persisted": False, "persist_reason": "no config file was loaded"}
        if os.path.abspath(path).startswith("/data/"):
            return {"persisted": False,
                    "persist_reason": "config is managed by Home Assistant (%s)" % path}
        try:
            cfg = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
            cfg.setdefault("settings", {})
            cfg["settings"].update(self.settings.to_dict())
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
                fh.write("\n")
            if os.path.exists(path):
                shutil.copyfile(path, path + ".bak")
            os.replace(tmp, path)
            LOG.info("settings persisted to %s", path)
            return {"persisted": True, "path": path}
        except Exception as exc:  # noqa: BLE001
            LOG.error("could not persist settings to %s: %s", path, exc)
            return {"persisted": False, "persist_reason": str(exc)}


# ------------------------- HTTP / web UI -------------------------
UI_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>EV Charge WT</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 header{padding:14px 18px;background:#161a22;border-bottom:1px solid #262c37;
        display:flex;justify-content:space-between;align-items:center}
 h1{font-size:16px;margin:0;font-weight:600}
 .wrap{padding:18px;display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
 .card{background:#161a22;border:1px solid #262c37;border-radius:10px;padding:14px}
 .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#8b93a5;margin:0 0 10px}
 .row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed #232833}
 .row:last-child{border:0}
 /* labels keep their own width; long values wrap right-aligned instead of
    squeezing the label onto a second line ("blocked" / "by") */
 .row>span:first-child{flex:0 0 auto;white-space:nowrap;padding-right:12px}
 .row>.v{text-align:right;min-width:0;overflow-wrap:anywhere}
 .v{font-variant-numeric:tabular-nums;font-weight:600}
 .big{font-size:26px;font-weight:700}
 .on{color:#4ade80}.off{color:#94a3b8}.warn{color:#fbbf24}.bad{color:#f87171}
 button{background:#233046;color:#e6e6e6;border:1px solid #33415c;border-radius:7px;
        padding:7px 11px;cursor:pointer;margin:2px}
 button.active{background:#2563eb;border-color:#3b82f6}
 input,select{background:#0f1115;color:#e6e6e6;border:1px solid #33415c;border-radius:6px;padding:5px;width:80px}
 label{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:3px 0}
 .badge{padding:2px 8px;border-radius:20px;font-size:11px;background:#233046}
</style></head><body>
<header><h1>EV Charge WT <span class="badge" id="modebadge">-</span></h1>
<div><span class="badge" id="safetybadge">-</span> <span class="badge" id="meterbadge">-</span> <span class="badge" id="ctrl">-</span> <span class="badge" id="age">-</span></div></header>
<div class="wrap">
 <div class="card"><h2>Decision</h2>
  <div class="big" id="d_charge">-</div>
  <div class="row" id="d_amp_row"><span>target current</span><span class="v" id="d_amp">-</span></div>
  <div class="row"><span>surplus</span><span class="v" id="d_surplus">-</span></div>
  <div class="row"><span>reason</span><span class="v" id="d_reason">-</span></div>
  <div class="row" id="d_safety_row" style="display:none"><span>stops 30 min</span><span class="v" id="d_safety">-</span></div>
  <button id="clearfault" style="display:none" title="Quittiert die Stoerung und gibt die
    Wallbox wieder frei. Ein Neustart des Dienstes tut dasselbe - der Riegel lebt nur im
    Speicher. Von selbst loest er sich nicht, solange die App laeuft.">Clear fault</button>
  <div class="row" id="d_cheap_row" style="display:none"><span>cheap window</span><span class="v" id="d_cheap">-</span></div>
  <div class="row" id="d_blocked_row" style="display:none"><span>blocked by</span><span class="v warn" id="d_blocked">-</span></div>
  <div class="row" id="d_grace_row" style="display:none"><span>grace</span><span class="v warn" id="d_grace">-</span></div>
  <div class="row" id="d_charger_row" style="display:none"><span>charger set to</span><span class="v" id="d_charger">-</span></div>
  <div class="row" id="d_manual_row" style="display:none"><span>manual for</span><span class="v warn" id="d_manual">-</span></div>
 </div>
 <div class="card"><h2>Solar / Grid / Battery</h2>
  <div class="row"><span>PV</span><span class="v" id="s_pv">-</span></div>
  <div class="row"><span>grid</span><span class="v" id="s_grid">-</span></div>
  <div class="row" id="s_bat_row"><span>battery</span><span class="v" id="s_bat">-</span></div>
  <div class="row"><span>battery SOC</span><span class="v" id="s_soc">-</span></div>
  <div class="row"><span>imported / exported</span><span class="v" id="s_energy">-</span></div>
  <div class="row" id="f_day_row" style="display:none" title="Lokale PV-Prognose fuer das Dach (Open-Meteo, 4 kWp Ost + 4 kWp West) - die Rest-Prognose fuer heute. Das wird noch NICHT gesteuert: die Zahl wird nur angezeigt und taeglich mitgeloggt, damit wir sehen, ob so eine Prognose fuer dieses Dach taugt.">
    <span>PV forecast today</span><span class="v" id="f_day">-</span></div>
  <div class="row" id="f_rest_row" style="display:none"><span>forecast rest of day</span><span class="v" id="f_rest">-</span></div>
  <div class="row" id="f_factor_row" style="display:none"><span>factor today</span><span class="v" id="f_factor">-</span></div>
  <div class="row" id="f_would_row" style="display:none"><span>rule would say</span><span class="v" id="f_would">-</span></div>
 </div>
 <div class="card"><h2>Vehicle</h2>
  <div class="row"><span>state</span><span class="v" id="c_state">-</span></div>
  <div class="row"><span>charging power</span><span class="v" id="c_power">-</span></div>
  <div class="row"><span>current / set</span><span class="v" id="c_amp">-</span></div>
  <div class="row"><span>session go-e</span><span class="v" id="c_sess">-</span></div>
  <div class="row"><span>session SDM</span><span class="v" id="c_sdm">-</span></div>
  <div class="row"><span>session SDM+Deye</span><span class="v" id="c_sdm_corr">-</span></div>
  <div class="row"><span>last session SDM</span><span class="v" id="c_sdm_last">-</span></div>
  <div class="row"><span>temperatures</span><span class="v" id="c_temp">-</span></div>
  <div class="row"><span>cable phases</span><span class="v" id="c_phases">-</span></div>
 </div>
 <div class="card"><h2>Mode &amp; settings</h2>
  <div id="modes"></div>
  <label>min current <input id="set_min_current" type="number" step="0.5"></label>
  <label>max current <input id="set_max_current" type="number" step="0.5"></label>
  <label title="Above this SOC the reserve above the buffer may carry a running charge: pv/minpv holds the car at the minimum current instead of switching it off when the sun goes, drawn from the battery - and lets go once the SOC is back down at this level, so the reserve is never spent below it.">buffer SOC <input id="set_buffer_soc" type="number" step="1"></label>
  <label title="Below this SOC the house battery has priority: what it is taking stays with it (the car charges on the real export only, which is not blocked), and while the battery supplies the car with nothing being exported the charge stops. From this SOC up the car gets the battery's charging share too, so it outranks the battery's charging instead of waiting for it to finish.">priority SOC <input id="set_priority_soc" type="number" step="1"></label>
  <label>plan kWh <input id="set_plan_energy_kwh" type="number" step="1"></label>
  <label>plan by <input id="set_plan_deadline" type="text" placeholder="07:30"></label>
  <label>cheap hours <input id="set_cheap_hours" type="text" style="width:140px" placeholder="00:00-05:00"></label>
  <label>time zone <input id="set_timezone" type="text" style="width:140px" placeholder="Europe/Berlin"></label>
  <div style="margin-top:10px"><button id="save">Save</button>
   <button id="ctrlbtn">-</button></div>
  <div id="msg" style="margin-top:8px;color:#8b93a5"></div>
 </div>
 <div class="card"><h2>Modbus proxy</h2>
  <div class="big" id="p_state">-</div>
  <div class="row"><span>last good read</span><span class="v" id="p_ok">-</span></div>
  <div class="row"><span>backoff now</span><span class="v" id="p_backoff">-</span></div>
  <div class="row"><span>errors / reconnects</span><span class="v" id="p_err">-</span></div>
  <div class="row"><span>timeouts</span><span class="v" id="p_timeouts">-</span></div>
  <div class="row"><span>reads / writes</span><span class="v" id="p_rw">-</span></div>
  <div class="row"><span>requests (hit/miss)</span><span class="v" id="p_req">-</span></div>
  <div class="row"><span>uptime</span><span class="v" id="p_uptime">-</span></div>
  <div class="row"><span>last error</span><span class="v" id="p_lasterr">-</span></div>
 </div>
</div>
<script>
const MODES=["off","now","minpv","pv","cheap_hours","manual"];
const MODE_HINT={cheap_hours:"uses the cheap_hours window, plus PV when the sun turns up"};
function w(x){if(x===null||x===undefined)return "-";if(Math.abs(x)>=1000)return (x/1000).toFixed(2)+" kW";return Math.round(x)+" W";}
async function load(){
 const r=await fetch("api/state");const s=await r.json();
 const d=s.decision||{},site=s.site||{},c=s.charger||{};
 document.getElementById("modebadge").textContent=(s.mode||"-").replace(/_/g," ");
 const mb=document.getElementById("modebadge");
 mb.className="badge"+(s.mode==="manual"?" warn":"");
 const man=!!d.manual;
 const ctrl=document.getElementById("ctrl");
 ctrl.textContent=s.control_enabled?"CONTROL ON":"DRY RUN";
 ctrl.className="badge "+(s.control_enabled?"on":"warn");
 document.getElementById("age").textContent=s.age_s!==undefined?Math.round(s.age_s)+"s ago":"-";
 const su=(s.site_unchanged_s===undefined||s.site_unchanged_s===null)?0:s.site_unchanged_s;
   const cad=(s.site_cadence_s===undefined||s.site_cadence_s===null)?30:s.site_cadence_s;
   const fails=s.site_failures||0;
   const busy=cad<=60;                  /* active cadence: a vehicle is connected */
   const mbx=document.getElementById("meterbadge");
   mbx.textContent=fails>0?("meter: "+fails+" failed read"+(fails>1?"s":""))
     :(su<0?"meter: no reading"
     :(su<=cad*2?(busy?"meter live":("meter idle ("+Math.round(su)+"s ago)"))
     :(busy?(su>=300?("METER held "+Math.round(su/60)+" min"):("meter held "+Math.round(su)+" s"))
            :("meter idle "+Math.round(su/60)+" min"))));
   mbx.className="badge"+((fails>0||su<0||(busy&&su>cad*2))?" warn":"");
 const dc=document.getElementById("d_charge");
 dc.textContent=man?"MANUAL":(d.charge?"CHARGING":"not charging");
 dc.className="big "+(man?"warn":(d.charge?"on":"off"));
 document.getElementById("d_amp_row").style.display=man?"none":"";
 document.getElementById("d_amp").textContent=(d.target_current||0).toFixed(1)+" A";
 document.getElementById("d_surplus").textContent=w(d.surplus_w);
 document.getElementById("d_reason").textContent=d.reason||"-";
 const bl=document.getElementById("d_blocked_row");
 if(d.blocked_by){document.getElementById("d_blocked").textContent=d.blocked_by;bl.style.display="";}
 else{bl.style.display="none";}
 // grace countdown: the state carries the seconds left, the page ticks them down
 // locally so the number moves between the 30 s state refreshes.
 if(d.disable_in_s>0){window._graceDeadline=Date.now()/1000+d.disable_in_s;window._graceKind="stop";}
 else if(d.enable_in_s>0){window._graceDeadline=Date.now()/1000+d.enable_in_s;window._graceKind=d.plan_wait?"plan":"start";}
 else{window._graceDeadline=0;window._graceKind="";}
 graceTick();
 // manual mode: show what the wallbox is actually set to, and how long we have been out of the way
 const cr=document.getElementById("d_charger_row");
 if(man){document.getElementById("d_charger").textContent=(c.max_current||0)+" A";cr.style.display="";}
 else{cr.style.display="none";}
 const mr=document.getElementById("d_manual_row");
 if(s.manual_since){const mins=Math.floor((Date.now()/1000-s.manual_since)/60);
  document.getElementById("d_manual").textContent=mins<60?mins+" min":Math.floor(mins/60)+"h "+(mins%60)+"m";
  mr.style.display="";}
 else{mr.style.display="none";}
 const g=s.garage||{};
 const gv=(g.pv_w===null||g.pv_w===undefined)?"-":w(g.pv_w);
 const spv=document.getElementById("s_pv");
 spv.textContent=w(site.pv_power_w)+" / "+gv;      /* SE (from its registers) / garage (from HA) */
 spv.title=(gv==="-")?"garage: no reading from Home Assistant"
   :("SE "+w(site.pv_power_w)+" (registers), garage "+w(g.pv_w)+" (HA"
     +(g.age_s!==null&&g.age_s!==undefined?", updated "+Math.round(g.age_s)+"s ago":"")
     +")");
 document.getElementById("s_grid").textContent=w(site.grid_power_w);
 document.getElementById("s_bat").textContent=(site.battery_power_w>0?"discharging ":"charging ")+w(Math.abs(site.battery_power_w||0));
 document.getElementById("s_soc").textContent=(site.battery_soc??0).toFixed(1)+" %";
 document.getElementById("s_energy").textContent=Math.round(site.grid_import_kwh??0)+" / "+Math.round(site.grid_export_kwh??0)+" kWh";
 // PV forecast (pv_forecast.py). STEP 1: shown and logged, deliberately NOT wired into any
 // decision - the owner wants to judge the forecast against his own roof first, so the row
 // reports what the rule *would* say and nothing acts on it.
 (function(){
   const f=s.forecast||{}, ids=["f_day_row","f_rest_row","f_factor_row","f_would_row"];
   ids.forEach(function(id){document.getElementById(id).style.display=f.enabled?"":"none";});
   if(!f.enabled){return;}
   const k=x=>(x===null||x===undefined)?"-":Number(x).toFixed(1)+" kWh";
   const n2=x=>(x===null||x===undefined)?"-":Number(x).toFixed(2);
   const day=document.getElementById("f_day"), rest=document.getElementById("f_rest"),
         fac=document.getElementById("f_factor"), wld=document.getElementById("f_would");
   const pl=(f.planes||[]).map(p=>p.kwp+" kWp @ "+p.azimuth+"deg/"+p.tilt+"deg").join(" + ");
   const age=(f.age_min===null||f.age_min===undefined)?null:Number(f.age_min);
   day.textContent=(f.today_kwh===undefined)?"keine Prognose":k(f.today_kwh);
   day.title="Ganze Prognose fuer heute: "+k(f.today_kwh)+" AC (PR "+f.pr+")"
     +" | bis jetzt erwartet: "+k(f.expected_so_far_kwh)
     +" | gemessen (AC, integriert): "+k(f.measured_kwh)
     +" | Quelle: Open-Meteo Stundensummen, "+pl
     +(age===null?"":" | Prognose "+Math.round(age)+" min alt")
     +(f.stale?" | STALE: letzte Aktualisierung fehlgeschlagen, es steht noch die alte Prognose":"")
     +" | NUR ANZEIGE UND LOG, kein Steuereingriff";
   rest.textContent=(f.remaining_kwh===undefined)?"-":(k(f.remaining_kwh)
     +(f.remaining_corrected_kwh!==undefined?" ("+k(f.remaining_corrected_kwh)+" korr.)":""));
   rest.title=(f.remaining_corrected_kwh!==undefined)
     ?("Rest der Prognose fuer heute "+k(f.remaining_kwh)+", mit dem heutigen Faktor "+n2(f.factor_today)
       +" auf "+k(f.remaining_corrected_kwh)+" korrigiert")
     :("Rest der Prognose fuer heute "+k(f.remaining_kwh)+" - noch kein Faktor, weil zu wenig Tag vorbei ist");
   fac.textContent=(f.factor_today===null||f.factor_today===undefined)?"noch keine Basis":n2(f.factor_today);
   fac.title="gemessen heute / Prognose fuer genau dieses Fenster. Unter 0.3 kWh Prognose wird kein "
     +"Faktor gebildet - das waere eine Division durch Rauschen (Vorlauf des Tages, truebe Stunden).";
   if(f.would_be_text){
     wld.textContent=f.would_be_text;
     wld.className="v"+(f.would_be==="battery"?" warn":"");
     wld.title="NUR ANZEIGE - diese Regel ist NICHT verdrahtet, es wird nichts geschaltet. "
       +"Rechnung: Rest-Prognose korrigiert "+k(f.remaining_corrected_kwh)+" minus angenommener "
       +"Hausverbrauch "+k(f.house_rest_kwh)+" = "+k(f.supply_after_house_kwh)+" gegen Akku-Bedarf "
       +"("+f.soc_target+" % Ziel - "+n2(f.soc)+" % jetzt) x "+f.capacity_kwh+" kWh = "
       +k(f.battery_need_kwh)+", mit Marge "+f.margin;
   }else{
     wld.textContent="-";
     wld.title="noch keine Entscheidungsgrundlage (Prognose oder SOC fehlt)";
   }
 })();
 document.getElementById("c_state").textContent=c.car_state||"-";
 document.getElementById("c_power").textContent=w(c.power);
 document.getElementById("c_amp").textContent=(c.currents&&c.currents[0]?c.currents[0].toFixed(1):"0")+" / "+c.max_current+" A";
 document.getElementById("c_sess").textContent=(s.session_kwh??0).toFixed(3)+" kWh";
 document.getElementById("c_sess").title="Integrated here from the wallbox's own per-phase "
   +"measurements over the measured cycle time, reset at every plug-in. The go-e's own "
   +"figures are on the card's tooltips; this is the number the SDM figure next to it is "
   +"there to check.";
 // Session energy, twice: the wallbox's own figure above, and the owner's own, measured at
 // the SDM630 in the garage (session_meter.py). That figure is the *meter's* view - the
 // garage PV feeds the same feeder and is deliberately not subtracted - so import, export
 // and net are all shown, and every finished session lands in the CSV.
 (function(){
   const sd=s.sdm||{}, ss=sd.session||null, sl=sd.last||null;
   const k=x=>(x===null||x===undefined)?"-":Number(x).toFixed(3);
   const el=document.getElementById("c_sdm"), le=document.getElementById("c_sdm_last");
   const ce=document.getElementById("c_sdm_corr");
   // A frozen meter must be visible, not silently read as 0 kWh: the SDM630 is polled by Home
   // Assistant, and that polling can stop without anything here failing.
   const ageTxt=(sd.read_age_s===null||sd.read_age_s===undefined)?"unknown age"
     :(sd.read_age_s<3600?Math.round(sd.read_age_s)+" s":(sd.read_age_s/3600).toFixed(1)+" h");
   const staleTxt=sd.stale?(" | STALE: the meter has not been read for "+ageTxt
     +" - the SDM630 polling in Home Assistant is not running"):"";
   if(!sd.enabled){ el.textContent="off"; el.title="SDM session meter disabled in config.json";
     ce.textContent="off"; ce.title="SDM session meter disabled in config.json"; }
   else if(ss){
     el.textContent=ss.waiting_for_meter?"waiting":k(ss.import_kwh)+" kWh";
     el.title="measured at the garage SDM630 | import since plug-in "+k(ss.import_kwh)+" kWh"
       +" | export (garage PV) "+k(ss.export_kwh)+" kWh | net "+k(ss.net_kwh)+" kWh"
       +" | running "+k(ss.duration_h)+" h since "+ss.started
       +" | the garage PV feeds the same feeder and is deliberately NOT subtracted"
       +" | meter reading "+(sd.read_age_s===null?"of unknown age":Math.round(sd.read_age_s)+" s old")
       +(ss.waiting_for_meter?" | WAITING for the first meter reading":"")
       +(ss.rebased?" | counter was reset "+ss.rebased+"x, energy before that kept":"")
       +(ss.relatched?" | baseline latched at process start":"")+staleTxt;
     // The third figure the owner asked for: the meter's one plus the garage PV's own
     // counter. It is the *upper* of the two SDM figures - car = import - export + garage PV
     // - and only as good as that counter, which may sit up to ~8 % above the meter's figure
     // (an UPPER bound: the meter cannot count the branch's own standby loads, so its export
     // counter under-reads by exactly what they eat - see the session meter's docstring).
     ce.textContent=ss.waiting_for_meter?"waiting":k(ss.corrected_kwh)+" kWh";
     ce.title="SDM figure corrected by the garage PV | net "+k(ss.net_kwh)+" kWh + garage PV "
       +k(ss.pv_kwh)+" kWh = "+k(ss.corrected_kwh)+" kWh"
       +" | source: "+(ss.pv_source==="counter"
         ?"the inverter's own counter - it can sit up to ~8 % above what the meter counted, because the meter cannot count the branch's own standby loads"
         :(ss.pv_waiting
           ?"no counter reading yet (its poller sleeps at night) - correction currently 0"
           :"no counter reading (its poller sleeps at night, PV = 0) - correction taken as 0"))
       +" | car = import - export + garage PV, so this sits above the plain SDM figure"
       +(sd.pv_artefacts?(" | inverter counter reported 0.00 "+sd.pv_artefacts+"x, ignored"):"");
   }
   else{
     el.textContent=sd.stale?"stale":((sd.import_kwh===null)?"no reading":"0.000 kWh");
     el.title="no car session right now | SDM630 counters: import "+k(sd.import_kwh)
       +" kWh, export "+k(sd.export_kwh)+" kWh | meter reading "+ageTxt+" old"
       +(sd.read_failures?(" | "+sd.read_failures+" read failure(s)"):"")+staleTxt;
     ce.textContent="-";
     ce.title="no car session right now | garage PV counter: "
       +((sd.pv_energy_kwh===null||sd.pv_energy_kwh===undefined)?"no reading"
         :k(sd.pv_energy_kwh)+" kWh"+(sd.pv_age_s===null||sd.pv_age_s===undefined?"":" ("
           +(sd.pv_age_s<3600?Math.round(sd.pv_age_s)+" s":(sd.pv_age_s/3600).toFixed(1)+" h")
           +" old)"))
       +(sd.pv_artefacts?(" | reported 0.00 "+sd.pv_artefacts+"x and was ignored"):"");
   }
   if(sl){
     le.textContent=k(sl.import_kwh)+" kWh";
     le.title="last finished session, measured at the SDM630 | import "+k(sl.import_kwh)+" kWh"
       +" | export "+k(sl.export_kwh)+" kWh | net "+k(sl.net_kwh)+" kWh"
       +" | "+k(sl.duration_h)+" h, "+sl.started+" to "+sl.ended
       +" | corrected by the garage PV: "+k(sl.corrected_kwh)+" kWh (garage PV "
       +k(sl.pv_kwh)+" kWh, source "+(sl.pv_source||"n/a")+")"
       +" | the wallbox's own session figure was "+k(sl.goe_session_kwh)+" kWh"
       +" | "+sd.rows+" row(s) in "+sd.csv+(sl.note?(" | "+sl.note):"");
   } else { le.textContent="-"; le.title="no finished session recorded yet ("+sd.csv+")"; }
 })();
 document.getElementById("c_temp").textContent=(c.temperatures||[]).slice(0,4).map(x=>Math.round(x)+"C").join(" ");
 // Cable phases: the number every power threshold depends on, and whether it was
 // measured from current that actually flowed or is still the configured assumption.
 const pn=d.phases||0;
 const cph=document.getElementById("c_phases");
 cph.textContent=pn?(pn+"p \u00b7 "+(d.phases_checked?"checked":"assumed")):"-";
 cph.className="v"+((pn&&!d.phases_checked)?" warn":"");
 cph.title="assumed = one phase until a charge has actually flowed (the owner's rule: try at the 1-phase minimum first)"
   +" | checked = measured from the phases that carried current, remembered until the car is unplugged";
 // Modbus proxy card: the headline answers "is something wrong with the link?"
 const px=s.proxy||{};
 const pst=document.getElementById("p_state");
 if(!px.text){pst.textContent="not configured";pst.className="big off";}
 else{pst.textContent=px.text;pst.className="big "+(px.level==="ok"?"on":(px.level==="warn"?"warn":"bad"));}
 const num=(v)=>v===null||v===undefined?"-":Math.round(v);
 const p_ok=document.getElementById("p_ok");
 p_ok.textContent=(px.ok_age_s===null||px.ok_age_s===undefined)?"never":(Math.round(px.ok_age_s)+" s ago");
 p_ok.className="v"+((px.ok_age_s!==null&&px.ok_age_s!==undefined&&px.backoff_s===0&&px.ok_age_s>200)?" warn":"");
 const p_bo=document.getElementById("p_backoff");
 p_bo.textContent=(px.backoff_s>0?Math.round(px.backoff_s)+" s":"0 s");
 p_bo.className="v"+(px.backoff_s>0?" warn":"");
 document.getElementById("p_err").textContent=num(px.errors)+" / "+num(px.reconnects);
 document.getElementById("p_timeouts").textContent=num(px.timeouts);
 const p_rw=document.getElementById("p_rw");
 p_rw.textContent=num(px.reads)+" / "+num(px.writes);
 p_rw.className="v"+(px.writes>0?" bad":"");
 document.getElementById("p_req").textContent=num(px.requests)+" ("+num(px.hits)+"/"+num(px.misses)+")";
 document.getElementById("p_uptime").textContent=px.uptime_s?(Math.floor(px.uptime_s/3600)+"h "+Math.floor((px.uptime_s%3600)/60)+"m"):"-";
 const p_le=document.getElementById("p_lasterr");
 const ago=(sec)=>{ if(sec==null) return "";
   if(sec<90) return "vor "+Math.round(sec)+" s";
   if(sec<3600) return "vor "+Math.round(sec/60)+" min";
   if(sec<5400) return "vor einer Stunde";
   if(sec<79200) return "vor "+Math.round(sec/3600)+" Stunden";
   return "vor "+Math.round(sec/86400)+" Tagen"; };
 p_le.textContent=px.last_error?((px.last_error_age_s!=null?(ago(px.last_error_age_s)+" \u00b7 "):"")
   +px.last_error):"-";
 p_le.title=px.last_error?((px.last_error_age_s!=null?("aufgetreten "+ago(px.last_error_age_s)
   +" ("+new Date((px.last_error_at||0)*1000).toLocaleTimeString()+" Uhr) | "):"")+px.last_error):"";
 p_le.className="v"+(px.last_error?" warn":"");
 const st=s.settings||{};
 // Charge-switching safety: how many on/off edges the wallbox has really seen, and the
 // fault latch that stops the app writing to it at all.
 const sf=s.safety||{};
 const sbadge=document.getElementById("safetybadge");
 if(sf.fault){sbadge.textContent="ST\u00d6RUNG";sbadge.className="badge bad";}
 else{sbadge.textContent="";sbadge.className="badge";}
 const srow=document.getElementById("d_safety_row");
 const sch=document.getElementById("d_safety");
 const changes=sf.changes||0, limit=sf.threshold||5;
 sch.textContent=changes+" / "+limit+" in "+(sf.window_min||30)+" min";
 sch.className="v"+(sf.fault?" bad":(changes>=Math.max(1,limit-2)?" warn":""));
 sch.title="charging stops actually written to the wallbox in the sliding window"
   +(sf.fault?(" | FAULT since "+(sf.fault_min_ago!=null?Math.round(sf.fault_min_ago)+" min ago":"just now")+": "+sf.fault_reason):"");
 srow.style.display=(changes>0||sf.fault)?"":"none";
 const cfb=document.getElementById("clearfault");
 cfb.style.display=sf.fault?"":"none";
 cfb.onclick=()=>post("api/safety/clear",{});
 // cheap window row: which hours, in whose clock, and how long until it opens
 const cw=s.cheap_window||{};
 const dcr=document.getElementById("d_cheap_row");
 if(!cw.windows||!cw.windows.length){dcr.style.display="none";}
 else{
  const nx=cw.inside?"open now":((cw.starts_in_min!=null)?("opens in "+Math.floor(cw.starts_in_min/60)+"h "+String(cw.starts_in_min%60).padStart(2,"0")+"m"):"");
  document.getElementById("d_cheap").textContent=cw.windows.join(", ")+" | "+cw.local+" "+cw.tz+" | "+nx;
  dcr.style.display="";
 }
 for(const k of ["min_current","max_current","buffer_soc","priority_soc","plan_energy_kwh","plan_deadline","cheap_hours","timezone"]){
   const el=document.getElementById("set_"+k); if(el&&st[k]!==undefined) el.value=Array.isArray(st[k])?st[k].join(","):st[k];
 }
 // Who gets the sun: the two battery bands, stated where the numbers are. Without this
 // the rule is invisible - the app just appears to charge at odd times.
 const BAT_HELP="The house battery has three bands. Below priority SOC it has priority: its "
  +"charging share stays with it and the car charges on the real export (the battery's draw is "
  +"already missing there, and the car is NOT blocked). From priority SOC up the car gets that "
  +"share too, so it outranks the battery's charging. Above buffer SOC the reserve above the "
  +"buffer may also carry a running charge: pv/minpv holds the car at the minimum current "
  +"instead of switching it off, until the SOC is back down at the buffer. The car is never "
  +"STARTED on battery energy.";
 (function(){
  const srow2=document.getElementById("s_bat_row"), bsoc=site.battery_soc;
  const bbuf=st.buffer_soc, bpri=st.priority_soc;
  const intake=w(Math.max(0,-(site.battery_power_w||0)));
  let band;
  if(bsoc===null||bsoc===undefined){band="Now: no SOC reading - the car gets the real export only.";}
  else if(bpri!==undefined&&bsoc<bpri){band="Now: SOC "+bsoc.toFixed(1)+"% < priority "+bpri
   +"% - the battery has priority, its "+intake+" stay with it, the car uses the real export only.";}
  else if(bbuf!==undefined&&bsoc>bbuf){band="Now: SOC "+bsoc.toFixed(1)+"% > buffer "+bbuf
   +"% - the car gets the battery's "+intake+" on top, and a running charge is carried at the "
   +"minimum current until the SOC is back at "+bbuf+"%.";}
  else{band="Now: SOC "+bsoc.toFixed(1)+"% - from priority "+(bpri!==undefined?bpri:"?")
   +"% up the car gets the battery's "+intake+" on top of the real export.";}
  if(bsoc!==null&&bsoc!==undefined&&bpri!==undefined&&bsoc<=bpri){band+=" A discharge into the "
   +"car with nothing exported stops the charge.";}
  // String.fromCharCode(10,10) is deliberate: this file is a Python triple-quoted string,
  // so a backslash-n written in the source becomes a real newline in the served JS and
  // kills the whole page script with a SyntaxError.
  srow2.title=BAT_HELP+String.fromCharCode(10,10)+band;
 })();
 const md=document.getElementById("modes");md.innerHTML="";
 for(const m of MODES){const b=document.createElement("button");b.textContent=m.replace(/_/g," ");
   if(MODE_HINT[m])b.title=MODE_HINT[m];
   if(m===s.mode)b.className="active";b.onclick=()=>post("api/mode",{mode:m});md.appendChild(b);}
 const cb=document.getElementById("ctrlbtn");
 cb.textContent=s.control_enabled?"Disable control":"Enable control";
 cb.onclick=()=>post("api/settings",{control_enabled:!s.control_enabled});
}
function graceTick(){
 const row=document.getElementById("d_grace_row");
 if(!window._graceDeadline){row.style.display="none";return;}
 const left=window._graceDeadline-Date.now()/1000;
 if(left<=0){row.style.display="none";return;}
 const fmt=left>=90?Math.floor(left/60)+" min "+String(Math.round(left%60)).padStart(2,"0")+" s":Math.round(left)+" s";
 const pre=window._graceKind==="stop"?"stopping charging in ":(window._graceKind==="plan"?"plan: must start charging in ":"starting to charge in ");
 const tail=window._graceKind==="stop"?" if it doesn't get better":(window._graceKind==="plan"?" to be ready in time":" if the surplus holds");
 document.getElementById("d_grace").textContent=pre+fmt+tail;
 row.style.display="";
}
setInterval(graceTick,1000);
async function post(path,body){
 const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
 document.getElementById("msg").textContent=await r.text();load();
}
document.getElementById("save").onclick=()=>{
 const keys=["min_current","max_current","buffer_soc","priority_soc","plan_energy_kwh","plan_deadline","cheap_hours","timezone"];
 const o={};for(const k of keys){const v=document.getElementById("set_"+k).value;if(v!=="")o[k]=v;}
 post("api/settings",o);
};
load();setInterval(load,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    service: Service = None  # type: ignore

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        svc = self.service
        if path in ("/", "/index.html"):
            return self._send(200, UI_HTML.encode(), "text/html; charset=utf-8")
        with svc._lock:
            snap = json.loads(json.dumps(svc.state, default=str))
        if "last_cycle" in snap and snap["last_cycle"]:
            snap["age_s"] = round(time.time() - snap["last_cycle"], 1)
        if path.startswith("/api/state"):
            return self._send(200, snap)
        if path.startswith("/api/settings"):
            return self._send(200, svc.settings.to_dict())
        if path.startswith("/api/health"):
            ok = bool(snap.get("last_cycle")) and (time.time() - snap["last_cycle"]) < 60
            return self._send(200 if ok else 503,
                              {"status": "ok" if ok else "stale", "cycles": snap.get("cycles"),
                               "age_s": snap.get("age_s"), "errors": snap.get("errors")})
        if path.startswith("/api/config"):
            return self._send(200, {"config": {k: v for k, v in svc.config.items()
                                               if k not in ("logging",)}})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid json"})
        if path.startswith("/api/settings"):
            return self._send(200, self.service.update_settings(payload))
        if path.startswith("/api/safety/clear"):
            self.service.safety.clear()
            LOG.warning("safety fault cleared by request")
            return self._send(200, {"cleared": True,
                                    **self.service.safety.as_state()})
        if path.startswith("/api/mode"):
            mode = payload.get("mode") or parse_qs(urlparse(self.path).query).get("mode", [""])[0]
            return self._send(200, self.service.update_settings({"mode": mode}))
        return self._send(404, {"error": "not found"})


def start_http(service: Service, host: str, port: int) -> ThreadingHTTPServer:
    Handler.service = service
    srv = ThreadingHTTPServer((host, port), Handler)
    t = threading.Thread(target=srv.serve_forever, name="http", daemon=True)
    t.start()
    LOG.info("web UI + REST on http://%s:%d/", host, port)
    return srv


# Home Assistant add-on options are a flat dict keyed by the add-on schema;
# translate them onto the nested service configuration.
ADDON_OPTION_MAP = {
    "site_host": ("site", "host"),
    "site_port": ("site", "port"),
    "site_unit": ("site", "unit"),
    "battery_capacity_kwh": ("site", "battery_capacity_kwh"),
    "charger_host": ("charger", "host"),
    "interval_s": ("interval_s",),
    "http_port": ("http", "port"),
    "mqtt_enabled": ("mqtt", "enabled"),
    "mqtt_host": ("mqtt", "host"),
    "mqtt_port": ("mqtt", "port"),
    "mqtt_user": ("mqtt", "user"),
    "mqtt_password": ("mqtt", "password"),
    "log_level": ("logging", "level"),
    "mode": ("settings", "mode"),
    "min_current": ("settings", "min_current"),
    "max_current": ("settings", "max_current"),
    "phases": ("settings", "phases"),
    "enable_delay_s": ("settings", "enable_delay_s"),
    "disable_delay_s": ("settings", "disable_delay_s"),
    "buffer_soc": ("settings", "buffer_soc"),
    "priority_soc": ("settings", "priority_soc"),
    "residual_power_w": ("settings", "residual_power_w"),
    "cheap_hours": ("settings", "cheap_hours"),
    "plan_energy_kwh": ("settings", "plan_energy_kwh"),
    "plan_deadline": ("settings", "plan_deadline"),
    "control_enabled": ("settings", "control_enabled"),
}


def _apply_flat_options(cfg: Dict, options: Dict) -> None:
    for key, value in options.items():
        path = ADDON_OPTION_MAP.get(key)
        if path is None:
            continue
        if len(path) == 1:
            cfg[path[0]] = value
        else:
            cfg.setdefault(path[0], {})[path[1]] = value


def load_config() -> tuple:
    """Read the configuration, returning (config, path).

    The path matters: update_settings writes changes back into the file the settings
    came from, so the app has to remember which candidate it actually loaded.
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    candidates = ["/data/options.json",
                  os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("could not read %s: %s", path, exc)
            continue
        if path.startswith("/data/"):
            _apply_flat_options(cfg, user)
        else:
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        LOG.info("loaded configuration from %s", path)
        return cfg, path
    return cfg, None


def setup_logging(cfg: Dict) -> None:
    lvl = getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(lvl)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    path = cfg.get("file")
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("file logging disabled: %s", exc)


def main() -> int:
    cfg, cfg_path = load_config()
    setup_logging(cfg.get("logging", {}))
    LOG.info("EV-Charger WT starting")
    svc = Service(cfg, cfg_path)
    http_cfg = cfg.get("http") or {}
    start_http(svc, http_cfg.get("host", "0.0.0.0"), int(http_cfg.get("port", 7080)))

    mqtt_cfg = cfg.get("mqtt") or {}
    if mqtt_cfg.get("enabled"):
        try:
            from .mqtt import HomeAssistantMqtt
            svc.mqtt = HomeAssistantMqtt(svc, mqtt_cfg)
            svc.mqtt.start()
            svc.state["mqtt_connected"] = True
        except Exception as exc:  # noqa: BLE001
            LOG.error("MQTT disabled: %s", exc)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(*_a):
        LOG.info("shutdown requested")
        loop.call_soon_threadsafe(svc._stop.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(svc.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        svc.site.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
