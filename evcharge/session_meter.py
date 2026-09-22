"""Second session-energy figure, measured at the SDM630 that sits in the garage.

The wallbox's own session figure under-reads on this plant, so this keeps a parallel one
from the meter on the garage feeder:

* **plug-in** - latch the meter's kWh counters,
* **while connected** - the difference is the running session,
* **unplug** - freeze it as the "last session" and append a row to a CSV.

The SDM630 measures the garage feeder, and the garage PV feeds into that same feeder, so
the figure is the *meter's* view of the session: what the car drew minus what the garage PV
delivered at that moment. The owner accepts that on purpose ("das nehme ich in Kauf und
möchte das nicht herausrechnen"), so nothing here corrects it - but the export counter is
recorded next to the import one, so the PV share stays visible instead of vanishing into a
single number.

The owner also wants a **third** figure for his analysis: the meter's one, *corrected* by the
garage PV's own counter. The balance on that feeder is

    car = import - export + garage PV during the session

so `import - export` is the part the meter can prove (the lower reading) and adding the
inverter's generation closes it. That sum is only as good as the inverter's counter, and
this inverter's counter is *known* to be sloppy here:

* over 21.09.2026 05:00-17:00 UTC the inverter said 4.18 kWh while the meter saw 3.88 kWh leave
  the branch: **at most 8 %**, and an upper bound rather than the inverter's error. That branch
  permanently carries a router, the garage door and the wallbox's standby, and the meter cannot
  *count* loads that small (start current 0.04 A = ~9 VA), so its export counter under-reads the
  PV by exactly what they consume. The 0.30 kWh gap equals 25 W of permanent load over those
  12 h, and the owner's own estimate (router ~10 W + wallbox standby ~5 W) already covers
  0.18 kWh of it - the inverter's real error lies somewhere between ~0 % and +8 %;
* and for the first minutes after every wake-up the register reads **0.00 kWh** (poller
  journal 05:16:53 `0.00 kWh`, four minutes later the real 279.56 kWh). On a lifetime counter
  that is an artefact, not a rollover: a reading below the running maximum is therefore
  *ignored* and counted in `pv_artefacts`, so a frozen correction is visible instead of
  silently wrong.

If no counter reading is available for a session (the logger sleeps at night, when the PV is
genuinely zero), the correction falls back to **0** and the row says so - the night figure is
then the meter's figure, which is exactly right while nothing is being generated.

Robustness, each pinned by a test:

* a counter that **drops** (device reset, rollover) rebases the session and flags it with
  `rebased`, rather than reporting a negative figure;
* a single cycle without the car does **not** end a session - the connector read can flicker;
* a session that is already running when the process starts is marked `relatched`, because
  its baseline is only as old as the process, and the CSV row says so;
* every failure path is a *reading* failure and never touches the car.

The meter writes nothing to the charger: it is a measurement, not a control.

Pure logic - the clock and the meter readings are injected, so the whole state machine is
testable without hardware (`tests/test_session_meter.py`).
"""
from __future__ import annotations

import csv
import logging
import os
import time
from typing import Callable, Dict, Optional

LOG = logging.getLogger("evcharge.sdm")

CSV_HEADER = ["started_utc", "ended_utc", "duration_h", "sdm_import_kwh", "sdm_export_kwh",
              "sdm_net_kwh", "goe_session_kwh", "rebased", "note",
              "garage_pv_kwh", "sdm_corrected_kwh", "pv_source"]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class SessionMeter:
    """Latches the SDM counters at plug-in and turns the difference into a session figure."""

    def __init__(self, read: Callable[[], Optional[Dict]], csv_path: str,
                 unplug_cycles: int = 2, enabled: bool = True, stale_s: float = 600.0,
                 now_fn: Callable[[], float] = time.time, pv_enabled: bool = True,
                 pv_stale_s: float = 900.0):
        # -> {"import_kwh": f, "export_kwh": f, "age_s": f,
        #     "pv_energy_kwh": f|None, "pv_age_s": f|None}
        self.read = read
        self.csv_path = csv_path
        # The garage PV's own lifetime counter (the inverter's register, published by its
        # poller into Home Assistant). Optional: without it the third session figure simply
        # stays the meter's figure and the CSV row says why.
        self.pv_enabled = bool(pv_enabled)
        self.pv_stale_s = float(pv_stale_s)
        self.unplug_cycles = max(1, int(unplug_cycles))
        self.enabled = bool(enabled)
        self.stale_s = float(stale_s)
        self.now = now_fn
        # A slow counter does not change for hours and Home Assistant does not rewrite an
        # unchanged state, so the counter's own timestamp says nothing about whether the
        # meter is still being read. Freshness therefore comes from a value that *moves on
        # every poll* - a phase voltage, whose jitter is always a new value. Do not use the
        # meter's power or current for this: at night they sit at 0.00 W / 0.0 A, are never
        # rewritten, and a perfectly healthy meter then looks dead.
        self.fresh = False
        self.stale_reads = 0
        self.import_kwh: Optional[float] = None     # last good meter readings
        self.export_kwh: Optional[float] = None
        self.read_age_s: Optional[float] = None
        self.read_failures = 0
        self.reads = 0
        self.pv_energy_kwh: Optional[float] = None   # last plausible inverter counter
        self.pv_age_s: Optional[float] = None
        self.pv_fresh = False
        self.pv_reads = 0
        self.pv_artefacts = 0                        # readings below the running maximum
        self.pv_failures = 0
        self.rows = 0
        self.session: Optional[Dict] = None
        self.last: Optional[Dict] = self._load_last()
        self._misses = 0                      # consecutive cycles without the car
        self._seen_unplugged = False          # so a session running at start is flagged

    # -- meter readings ---------------------------------------------------
    def _take_reading(self) -> None:
        try:
            got = self.read()
        except Exception as exc:  # noqa: BLE001 - a measurement must never break the loop
            self.read_failures += 1
            LOG.warning("SDM session meter read failed: %s", exc)
            return
        if isinstance(got, dict):
            # Independent of the meter's own freshness: the inverter's counter has its own
            # age, and at night it is legitimately silent while the meter is fine.
            self._accept_pv(got)
        if not got or got.get("import_kwh") is None:
            self.read_failures += 1
            return
        age = got.get("age_s")
        self.read_age_s = age
        if age is not None and age > self.stale_s:
            # The meter is not being read any more: keep the counters for display, but a
            # session must not be measured from a frozen number.
            self.stale_reads += 1
            self.fresh = False
            if self.stale_reads == 1:
                LOG.warning("SDM readings are stale (last update %.1f min ago) - the session "
                            "figure waits for a fresh one", age / 60.0)
            return
        self.import_kwh = float(got["import_kwh"])
        exp = got.get("export_kwh")
        self.export_kwh = float(exp) if exp is not None else None
        self.fresh = True
        self.stale_reads = 0
        self.read_failures = 0
        self.reads += 1

    def _accept_pv(self, got: Dict) -> None:
        """One garage-PV counter reading, guarded against the inverter's own slip-ups."""
        if not self.pv_enabled:
            return
        raw = got.get("pv_energy_kwh")
        if raw is None:
            self.pv_failures += 1
            if self.pv_failures == 1:
                LOG.info("no garage-PV counter reading - the corrected session figure waits "
                         "for the inverter's poller")
            return
        self.pv_failures = 0
        age = got.get("pv_age_s")
        self.pv_age_s = age
        if age is not None and age > self.pv_stale_s:
            self.pv_fresh = False
            return
        val = float(raw)
        if val <= 0.0 or (self.pv_energy_kwh is not None and val < self.pv_energy_kwh):
            # Two slips of this register, both measured on the plant: it reads **0.00 kWh**
            # for minutes after the logger wakes up (journal 05:16:53), and a value below the
            # running maximum never happens on a lifetime counter. Either one taken as a
            # baseline/reading would invent or destroy energy - the 0.00 as a *baseline* would
            # turn the next real reading into a ~279 kWh correction. So neither is accepted,
            # and both are counted so a frozen correction stays visible.
            self.pv_artefacts += 1
            if self.pv_artefacts == 1 or self.pv_artefacts % 10 == 0:
                LOG.warning("garage-PV counter reported %s kWh (running maximum %s) - "
                            "ignored as an artefact, %d so far", val, self.pv_energy_kwh,
                            self.pv_artefacts)
            self.pv_fresh = False
            return
        self.pv_energy_kwh = val
        self.pv_fresh = True
        self.pv_reads += 1

    @property
    def readings_ok(self) -> bool:
        """Usable for a session: a value, and one that is not frozen."""
        return self.import_kwh is not None and self.fresh

    # -- session figure ---------------------------------------------------
    def _figures(self) -> Dict:
        """The running session's numbers, from the latched baselines to now."""
        s = self.session
        imp = s["import_kwh"]
        exp = s["export_kwh"]
        pv = s["pv_kwh"] if s["pv_known"] else None
        return {
            "import_kwh": round(imp, 3),
            "export_kwh": round(exp, 3),
            "net_kwh": round(imp - exp, 3),     # negative = the garage PV won that moment
            # The third figure: the meter's net plus what the garage PV fed into the same
            # feeder during this session. Without a counter reading the correction is 0 and
            # `pv_source` says so.
            "pv_kwh": None if pv is None else round(pv, 3),
            # A running session whose counter has not answered yet is reported the way a
            # finished one is - correction 0 - and `pv_waiting` says that this is still a
            # hope rather than a measurement.
            "pv_source": s["pv_source"] if s["pv_known"] else "assumed_zero",
            "pv_waiting": s["pv_source"] == "pending",
            "pv_late": s["pv_late"],
            "corrected_kwh": round((imp - exp) + (pv or 0.0), 3),
            "waiting_for_meter": not s["have_baseline"],
            "rebased": s["rebased"],
            "relatched": s["relatched"],
        }

    def _delta(self, current: Optional[float], key: str) -> float:
        """Difference since the baseline, rebasing (and flagging) if the counter dropped."""
        s = self.session
        base = s["base_" + key]
        if current is None or base is None:
            return s[key + "_kwh"]
        if current < base:
            # A reset or a rollover: freeze what was counted (it really did flow), start a
            # fresh baseline, and never report a negative session because of it.
            s["rebased"] += 1
            s["carry_" + key] = s[key + "_kwh"]
            s["base_" + key] = current
            LOG.warning("SDM %s counter dropped below its baseline (%s < %s) - rebased, "
                        "carrying %.3f kWh", key, current, base, s["carry_" + key])
            return s[key + "_kwh"]
        return s["carry_" + key] + (current - base)

    def _start(self, now: float) -> None:
        have = self.readings_ok
        self.session = {
            "started": _iso(now),
            "started_ts": now,
            "base_import": self.import_kwh,
            "base_export": self.export_kwh,
            "have_baseline": have,
            "import_kwh": 0.0,
            "export_kwh": 0.0,
            "base_pv": self.pv_energy_kwh,
            "pv_kwh": 0.0,
            "pv_known": False,
            "pv_source": "pending",       # pending | counter | assumed_zero
            "pv_late": False,             # counter woken mid-session: correction is partial
            "pv_artefacts0": self.pv_artefacts,
            "rebased": 0,
            # Energy counted before a counter reset has to survive the rebase (it really did
            # flow), so it is carried instead of being overwritten by the new baseline's delta.
            "carry_import": 0.0,
            "carry_export": 0.0,
            # The baseline can only be as old as this observation: if the car was already
            # plugged in when the process started, part of the session is not covered.
            "relatched": not self._seen_unplugged,
            "waited_for_meter": not have,     # the figure starts late, say so in the CSV
            "goe_max_kwh": 0.0,
        }
        LOG.info("SDM session started (baseline import %s kWh, export %s kWh%s)",
                 self.import_kwh, self.export_kwh,
                 "" if have else " - waiting for the first meter reading")

    def _advance(self, goe_session_kwh: float) -> None:
        s = self.session
        if not s["have_baseline"] and self.readings_ok:
            s["base_import"], s["base_export"] = self.import_kwh, self.export_kwh
            s["have_baseline"] = True
            LOG.info("SDM session baseline latched late (after plug-in): import %s kWh",
                     self.import_kwh)
        if s["have_baseline"]:
            s["import_kwh"] = self._delta(self.import_kwh, "import")
            s["export_kwh"] = self._delta(self.export_kwh, "export")
        # The garage PV's share of this session, from its own counter. Only the growth of a
        # counter that is *plausible* counts: `_accept_pv` never lets a reading go backwards,
        # and the age check keeps a frozen entity from looking like a still inverter.
        # A session that started while the logger was asleep (night) latches the baseline as
        # soon as a reading arrives - home-assistant-style "late baseline", the correction
        # then covers the rest of the session and the CSV row says so.
        if s["base_pv"] is None and self.pv_energy_kwh is not None:
            s["base_pv"] = self.pv_energy_kwh
            s["pv_late"] = True
            LOG.info("garage-PV counter baseline latched late (after plug-in): %.3f kWh - the "
                     "correction covers only the rest of this session", self.pv_energy_kwh)
        if s["base_pv"] is not None and self.pv_energy_kwh is not None:
            s["pv_kwh"] = max(0.0, self.pv_energy_kwh - s["base_pv"])
            s["pv_known"] = True
            s["pv_source"] = "counter"
        # The go-e figure is kept as a maximum: this app's own counter is reset when the
        # car goes away, and the CSV wants the session's final value either way.
        s["goe_max_kwh"] = max(s["goe_max_kwh"], float(goe_session_kwh or 0.0))

    def _finish(self, now: float) -> None:
        s = self.session
        note = ""
        if not s["have_baseline"] and self.stale_reads:
            note = ("meter readings were stale (frozen counter) - no figure, "
                    "see the SDM polling in Home Assistant")
        elif not s["have_baseline"]:
            note = "no meter reading during the session"
        elif s["waited_for_meter"]:
            note = "first meter reading arrived after plug-in - the figure starts late"
        if s["relatched"]:
            note = (note + "; " if note else "") + "baseline latched at process start"
        if s["rebased"]:
            note = (note + "; " if note else "") + "counter(s) rebased %d times" % s["rebased"]
        pv = s["pv_kwh"] if s["pv_known"] else None
        # A session that could not see the inverter's counter gets the meter's figure as its
        # third value - right while nothing is being generated (at night it is), and the note
        # says it was assumed rather than measured.
        pv_source = s["pv_source"] if s["pv_known"] else "assumed_zero"
        if s["have_baseline"] and pv_source == "assumed_zero":
            note = (note + "; " if note else "") + \
                "no garage-PV counter reading - correction taken as 0"
        if s["pv_late"]:
            note = (note + "; " if note else "") + \
                "garage-PV counter latched late - the correction covers only part of the session"
        dropped = self.pv_artefacts - s["pv_artefacts0"]
        if dropped:
            note = (note + "; " if note else "") + \
                "inverter counter reported 0.00 %dx (ignored)" % dropped
        result = {
            "started": s["started"],
            "ended": _iso(now),
            "duration_h": round(max(0.0, now - s["started_ts"]) / 3600.0, 3),
            "import_kwh": round(s["import_kwh"], 3),
            "export_kwh": round(s["export_kwh"], 3),
            "net_kwh": round(s["import_kwh"] - s["export_kwh"], 3),
            "goe_session_kwh": round(s["goe_max_kwh"], 3),
            "rebased": s["rebased"],
            "note": note,
            "pv_kwh": None if pv is None else round(pv, 3),
            "corrected_kwh": round((s["import_kwh"] - s["export_kwh"]) + (pv or 0.0), 3),
            "pv_source": pv_source,
        }
        self.last = result
        self._append_row(result)
        LOG.info("SDM session ended: %.3f kWh import, %.3f kWh export, %.3f kWh net over "
                 "%.2f h (go-e figure %.3f kWh) | garage PV %s kWh (%s) -> corrected %.3f kWh",
                 result["import_kwh"], result["export_kwh"], result["net_kwh"],
                 result["duration_h"], result["goe_session_kwh"],
                 "n/a" if pv is None else "%.3f" % pv, pv_source, result["corrected_kwh"])
        self.session = None
        self._misses = 0

    # -- CSV --------------------------------------------------------------
    def _append_row(self, r: Dict) -> None:
        try:
            path = os.path.expanduser(self.csv_path)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            fresh = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", newline="") as fh:
                w = csv.writer(fh)
                if fresh:
                    w.writerow(CSV_HEADER)
                w.writerow([r["started"], r["ended"], r["duration_h"], r["import_kwh"],
                            r["export_kwh"], r["net_kwh"], r["goe_session_kwh"],
                            r["rebased"], r["note"],
                            "" if r["pv_kwh"] is None else r["pv_kwh"],
                            r["corrected_kwh"], r["pv_source"]])
            self.rows += 1
        except OSError as exc:
            LOG.warning("could not append the SDM session row to %s: %s", self.csv_path, exc)

    def _load_last(self) -> Optional[Dict]:
        """The last session survives a restart because the CSV is the record of it."""
        try:
            path = os.path.expanduser(self.csv_path)
            if not os.path.exists(path):
                return None
            with open(path, newline="") as fh:
                rows = [r for r in csv.reader(fh) if r and r[0] != CSV_HEADER[0]]
            if not rows:
                return None
            r = rows[-1]
            self.rows = len(rows)
            # Rows written before the corrected figure existed simply have no value there.
            pv = r[9] if len(r) > 9 and r[9] != "" else None
            return {"started": r[0], "ended": r[1], "duration_h": float(r[2]),
                    "import_kwh": float(r[3]), "export_kwh": float(r[4]),
                    "net_kwh": float(r[5]), "goe_session_kwh": float(r[6]),
                    "rebased": int(float(r[7])), "note": r[8] if len(r) > 8 else "",
                    "pv_kwh": None if pv is None else float(pv),
                    "corrected_kwh": float(r[10]) if len(r) > 10 and r[10] != "" else None,
                    "pv_source": r[11] if len(r) > 11 else ""}
        except (OSError, ValueError, IndexError) as exc:
            LOG.warning("could not read the last SDM session from %s: %s", self.csv_path, exc)
            return None

    # -- per cycle --------------------------------------------------------
    def update(self, connected: bool, goe_session_kwh: float = 0.0) -> None:
        """One cycle: takes a meter reading and follows the plug/unplug edges."""
        if not self.enabled:
            return
        self._take_reading()
        now = self.now()
        if connected:
            # Note: `_seen_unplugged` is deliberately NOT reset here - it records whether this
            # process ever saw the car absent, which is what makes a session that was already
            # running at startup recognisable. Resetting it here would flag every session.
            self._misses = 0
            if self.session is None:
                self._start(now)
            self._advance(goe_session_kwh)
            return
        self._seen_unplugged = True
        if self.session is None:
            return
        self._misses += 1
        if self._misses >= self.unplug_cycles:
            self._finish(now)

    # -- publication ------------------------------------------------------
    def as_state(self) -> Dict:
        out: Dict = {
            "enabled": self.enabled,
            "import_kwh": None if self.import_kwh is None else round(self.import_kwh, 3),
            "export_kwh": None if self.export_kwh is None else round(self.export_kwh, 3),
            "read_age_s": self.read_age_s,
            "stale": bool(self.enabled and not self.fresh),
            "stale_s": self.stale_s,
            "stale_reads": self.stale_reads,
            "read_failures": self.read_failures,
            "reads": self.reads,
            "pv_enabled": self.pv_enabled,
            "pv_energy_kwh": None if self.pv_energy_kwh is None else round(self.pv_energy_kwh, 3),
            "pv_age_s": self.pv_age_s,
            "pv_stale": bool(self.pv_enabled and not self.pv_fresh),
            "pv_reads": self.pv_reads,
            "pv_artefacts": self.pv_artefacts,
            "csv": self.csv_path,
            "rows": self.rows,
        }
        if self.session is not None:
            s = self._figures()
            s.update({"started": self.session["started"],
                      "duration_h": round(max(0.0, self.now() - self.session["started_ts"]) / 3600.0, 3),
                      "goe_session_kwh": round(self.session["goe_max_kwh"], 3)})
            out["session"] = s
        else:
            out["session"] = None
        out["last"] = self.last
        return out
