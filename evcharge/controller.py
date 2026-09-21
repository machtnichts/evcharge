"""Charging controller - surplus and tariff decision logic for this one plant.

Modes
    off      never charge
    now      charge immediately at max current
    minpv    start at minimum current as soon as there is minimum PV surplus
    pv       follow PV surplus (ramps current up/down, battery buffer respected)

Settings cover the parts that matter for this installation:
    min/max current, phases, enable/disable thresholds and delays,
    battery buffer SOC (battery charges first), priority SOC,
    cheap-tariff windows, and a simple "charge to X kWh by HH:MM" plan.

The controller is pure logic: it takes a SiteState + ChargerState + settings and
returns a Decision. All hardware writes happen in the service layer, and only
when control is explicitly enabled.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime, timedelta
from typing import Dict, List, Optional

from .drivers.solaredge import SiteState
from .drivers.goe import ChargerState

MODE_OFF = "off"
MODE_NOW = "now"
MODE_MINPV = "minpv"
MODE_PV = "pv"
MODE_MANUAL = "manual"      # charger belongs to the user: the app issues zero writes
MODE_CHEAP = "cheap_hours"  # the cheap tariff window, plus PV whenever the sun turns up
MODES = (MODE_OFF, MODE_NOW, MODE_MINPV, MODE_PV, MODE_CHEAP, MODE_MANUAL)


@dataclass
class Settings:
    mode: str = MODE_PV
    min_current: float = 6.0            # A per phase
    max_current: float = 14.0           # A per phase (this plant: wallbox/car limit)
    phases: int = 1                     # charging phase(s) of the vehicle
    voltage_nominal: float = 230.0
    enable_threshold_w: float = 0.0     # surplus above this enables charging
    disable_threshold_w: float = 0.0    # surplus below this disables charging
    enable_delay_s: int = 60
    disable_delay_s: int = 180
    buffer_soc: float = 80.0            # below this the battery's charging share stays
                                        # with the battery (the car uses the real export);
                                        # at/above it the car gets that share too
    priority_soc: float = 55.0          # below this the battery has priority: its charging
                                        # share stays with it, and a discharge into the car
                                        # with nothing exported stops the charge
    residual_power_w: float = 0.0       # reserve kept below the surplus; 0 = off (the
                                        # meter already measures the house's own load)
    cheap_hours: List[str] = field(default_factory=lambda: ["00:00-05:00"])
    # The zone the cheap_hours window (and the plan deadline) are stated in. Empty means
    # the host's own clock; an IANA name is DST-correct through the standard library, so
    # "00:00-05:00" keeps meaning midnight-to-five on the house's wall as the seasons
    # change. A wrong zone here silently shifts every night-time charge by hours.
    timezone: str = ""
    cheap_price_per_kwh: float = 0.18
    grid_price_per_kwh: float = 0.28
    feed_in_per_kwh: float = 0.12
    control_enabled: bool = False       # master switch for hardware writes
    max_current_step: float = 1.0       # unused: the current follows the surplus at once
    min_change_interval_s: int = 30
    site_stale_s: float = 600.0         # refuse to act on a site reading older than this
    plan_energy_kwh: float = 0.0        # charge this much ...
    plan_deadline: str = ""             # ... by this local time (HH:MM)

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Decision:
    ts: float = field(default_factory=time.time)
    mode: str = MODE_PV
    charge: bool = False                # want the vehicle to charge
    manual: bool = False                # manual mode: the app is not deciding anything
    target_current: float = 0.0         # A per phase
    reason: str = ""
    surplus_w: float = 0.0
    available_w: float = 0.0
    granted_w: float = 0.0
    planned_kwh: float = 0.0
    plan_active: bool = False
    cheap_now: bool = False
    blocked_by: str = ""
    enable_in_s: float = 0.0            # s until the app wants to charge (grace or plan)
    disable_in_s: float = 0.0           # seconds left of the disable grace (0 = none)
    plan_wait: bool = False             # waiting for a deadline charge to be due
    actions: List[str] = field(default_factory=list)
    # Which phase count the maths used, and where it came from: measured from current
    # that actually flowed ("checked") or the configured assumption ("assumed"). The two
    # differ by a factor of three in every power threshold, so the UI shows both.
    phases: int = 0
    phases_checked: bool = False
    # House battery SOC the decision was made with, so _finalize can apply the buffer rule
    # (hold a running charge above buffer SOC) without being handed the site reading.
    soc: Optional[float] = None


def _parse_hm(text: str) -> Optional[dtime]:
    try:
        h, m = text.strip().split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def _in_window(now: dtime, window: str) -> bool:
    if "-" not in window:
        return False
    start_s, end_s = window.split("-", 1)
    start, end = _parse_hm(start_s), _parse_hm(end_s)
    if start is None or end is None:
        return False
    if start <= end:
        return start <= now < end
    return now >= start or now < end       # window crossing midnight


def plant_tz(name: str):
    """The zone the plant's wall-clock settings are stated in, or None for the host's.

    An IANA name (Europe/Berlin) resolves DST correctly, so a window written as
    00:00-05:00 means midnight-to-five on the house's wall in winter and in summer
    alike. An unresolvable name degrades to the host's clock instead of failing.
    """
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name.strip())
    except Exception:  # noqa: BLE001
        return None


def plant_now(name: str = "", now: Optional[datetime] = None) -> datetime:
    """Naive wall-clock time in the plant's zone.

    An aware input is converted; a naive input is taken as an already-local wall clock
    (which is what callers and tests pass when they mean a specific time of day).
    """
    tz = plant_tz(name)
    if now is not None:
        if now.tzinfo is not None:
            return (now.astimezone(tz) if tz else now.astimezone()).replace(tzinfo=None)
        return now
    return (datetime.now(tz) if tz else datetime.now()).replace(tzinfo=None)


def cheap_window_info(s, now: Optional[datetime] = None) -> dict:
    """What the UI needs to show that the cheap window is in the house's own hours.

    Returns the zone in use, the current wall-clock time there, the windows, whether one
    is open, and minutes until the next one opens - so "are these really my hours?" is
    answerable on screen instead of by reading the code.
    """
    tz = plant_tz(s.timezone)
    if now is not None and now.tzinfo is not None:
        local = (now.astimezone(tz) if tz else now.astimezone()).replace(tzinfo=None)
    else:
        local = now or (datetime.now(tz) if tz else datetime.now()).replace(tzinfo=None)
    windows = list(s.cheap_hours or [])
    inside = any(_in_window(local.time(), w) for w in windows)
    starts_in = None
    if windows and not inside:
        for w in windows:
            start = _parse_hm(w.split("-", 1)[0]) if "-" in w else None
            if start is None:
                continue
            mins = ((start.hour * 60 + start.minute)
                    - (local.hour * 60 + local.minute)) % 1440
            if mins == 0:
                mins = 1440
            starts_in = mins if starts_in is None else min(starts_in, mins)
    return {
        "tz": (s.timezone.strip() if s.timezone else "(host clock)"),
        "local": local.strftime("%H:%M"),
        "windows": windows,
        "inside": inside,
        "starts_in_min": starts_in,
    }


# How long current must be flowing before the phase count it reveals is believed. The
# opening cycles catch the phases coming up, not the count the car settles on.
RAMP_S = 20.0


class ChargingController:
    def __init__(self, settings: Settings, log=None):
        self.s = settings
        self._enable_since: Optional[float] = None
        self._disable_since: Optional[float] = None
        self._last_change: float = 0.0
        self._last_decision: Optional[Decision] = None
        self.charging_session_kwh: float = 0.0
        # Last phase count the go-e actually showed current on. Kept for as long as the
        # car stays plugged in: a charge started on a 3-phase cable then knows it is
        # 3-phase from the first cycle instead of doing the maths at 1 phase and
        # commanding 3x the current. Dropped on unplug, because that is when the cable
        # can change.
        self._known_phases: int = 0
        # When current started flowing in this charge, so the phase count is only believed
        # once it has settled (see _active_phases).
        self._flowing_since: Optional[float] = None

    # -- helpers ---------------------------------------------------------
    def _currents_bounds(self) -> tuple:
        lo = max(0.0, self.s.min_current)
        hi = max(lo, self.s.max_current)
        return lo, hi

    def _power_for_current(self, amps: float, phases: Optional[int] = None) -> float:
        return amps * (phases or self.s.phases) * self.s.voltage_nominal

    def _floor_w(self, min_start: float, charger: ChargerState) -> float:
        """The surplus the floor demands right now - asymmetric on purpose.

        While the wallbox is still switched off, starting needs the minimum *plus* the
        enable margin, so a surplus that merely touches the minimum does not open a session.
        While it is switched on, holding only needs the minimum *minus* the disable margin,
        so a passing cloud does not end one. Without the two margins a surplus sitting on
        the minimum starts and stops the charge every few minutes - the switching the safety
        counter exists to catch (measured: 5 stops in 30 minutes on 20.09.2026).
        """
        if charger.enabled:
            return max(0.0, min_start - self.s.disable_threshold_w)
        return min_start + self.s.enable_threshold_w

    def _active_phases(self, charger: ChargerState, now: Optional[datetime] = None) -> int:
        """Phase count to use in the power<->current maths.

        The owner's rule, and the reason this is not simply a setting: after a plug-in the
        cable is *assumed* to be one phase, so the controller tries a charge at the
        1-phase minimum (~1.4 kW) rather than waiting for a 3-phase one. Only current that
        actually flows settles the question, and the go-e reports the phases that carry it.
        Let the charge settle first: the opening cycles catch the phases coming up (a
        3-phase car can read 2 for a moment), and a count that is too low makes every
        power threshold too low with it. The highest count seen while the car stays
        plugged in is kept, and forgotten on unplug so a cable change is caught.
        """
        t = (now or datetime.now()).timestamp()
        if charger.charging and charger.phases:
            if self._flowing_since is None:
                self._flowing_since = t
            # What is flowing right now is used for the maths straight away: mid-ramp it
            # is nearer the truth than the 1-phase assumption (a 3-phase car reads 2 for a
            # moment), and it stops a 3 kW surplus setting 13 A on a 3-phase car.
            live = max(1, charger.phases)
            # Remembering is stricter, because the memory becomes the stand-in for the
            # *next* charge. Three phases is final evidence (the ramp can only under-report
            # as they come up); anything less waits until the charge has settled.
            if charger.phases >= 3 or (t - self._flowing_since) >= RAMP_S:
                self._known_phases = max(self._known_phases, charger.phases)
            return live
        self._flowing_since = None
        if not charger.connected:
            self._known_phases = 0
            return 1
        if self._known_phases:
            return self._known_phases
        return 1

    def _cheap_now(self, now: datetime) -> bool:
        return any(_in_window(now.time(), w) for w in self.s.cheap_hours or [])

    def _soc(self, site: SiteState) -> Optional[float]:
        return site.battery_soc

    # -- main decision ---------------------------------------------------
    def decide(self, site: SiteState, charger: ChargerState,
               now: Optional[datetime] = None, session_kwh: float = 0.0,
               site_stale_s: float = 0.0) -> Decision:
        now = now or plant_now(self.s.timezone)
        s = self.s
        d = Decision(mode=s.mode, cheap_now=self._cheap_now(now))

        # Which phase count are we deciding with, and do we actually know it? Asked here,
        # before any branch returns, so every path publishes the same answer to the UI.
        # `ph` is what the maths uses (what is flowing right now, or the assumption);
        # the decision *publishes* the cable's known count instead, because that is the
        # stable, meaningful thing to show: a car idling one phase is not a 1-phase cable.
        ph = self._active_phases(charger, now)
        d.phases = self._known_phases or 1
        d.phases_checked = self._known_phases > 0

        lo, hi = self._currents_bounds()
        # Surplus = exported power plus what the car itself currently draws.
        car_draw = charger.power if charger.charging else 0.0
        # Signed grid, not exporting_w: exporting_w is clamped at 0, so while the car
        # draws from the grid (cloud rolls in, kettle on) its own draw would count as
        # surplus, the decision would stay "charge" and the disable timer would never
        # engage. With the signed value the surplus goes negative in that case.
        #
        # Whatever the house battery is supplying is subtracted too: while the battery
        # covers part of the car, that power is not solar available for it. Without this
        # the target sits about one amp high and the battery drains quietly all day
        # (measured: 169 W discharge while the car took 3190 W from a 3677 W array).
        #
        # House battery: three bands, as specified by the owner for this plant.
        #
        # * below priority SOC the battery has priority: what it is *taking* stays with it
        #   and is not offered to the car. The car is not blocked - it charges from the real
        #   export, in which the battery's draw is already missing (the grid meter measures
        #   it), so a grey day with a half-full battery charges the car instead of pushing
        #   the sun into the grid.
        # * from priority SOC up the car gets that share too: it outranks the battery's
        #   charging instead of waiting for it to finish, and the battery settles at its
        #   level rather than creeping on to 100 %.
        # * above buffer SOC the battery may also *carry* the car: a running charge is held
        #   at the minimum current instead of being switched off when the sun goes, so
        #   pv/minpv does not stop the car while the battery still has room above its buffer
        #   (see _finalize). It ends when the charge falls back to the buffer.
        #
        # Only the intake changes hands in the surplus; the battery's *discharge* is
        # subtracted so the car never quietly drains the house battery. The one exception is
        # the hold above buffer SOC, which is a target-current rule, not a surplus rule.
        #
        # residual_power_w is a deliberate reserve *below* the surplus and is 0 on this
        # plant: the house's own consumption is already in the signed grid reading (the
        # meter measures it), so subtracting an estimate of it again would count the base
        # load twice and hold the charge about an amp low.
        soc = self._soc(site)
        d.soc = soc
        battery_share = (site.battery_charging_w
                         if (soc is not None and soc >= s.priority_soc) else 0.0)
        surplus = (-site.grid_power_w + car_draw - s.residual_power_w
                   - site.battery_discharging_w + battery_share)
        d.surplus_w = round(surplus, 1)

        # Battery discharge protection in PV mode: below priority SOC the house battery's
        # reserve is not the car's either. While the battery is supplying the car and
        # nothing is exported, the charge stops instead of draining it further. (The hold
        # above buffer SOC is the deliberate exception - there the reserve above the buffer
        # is meant to carry the car.)
        battery_low = soc is not None and soc <= s.priority_soc
        charging_from_battery = site.battery_discharging_w > 200 and site.exporting_w <= 0
        if battery_low and charging_from_battery and s.mode == MODE_PV:
            d.blocked_by = "battery at or below priority SOC (%.1f%%)" % soc

        if s.mode == MODE_MANUAL:
            # Manual mode: the charger belongs to the user. The app keeps reading and
            # reporting, but decides nothing - and hardware_actions() refuses every
            # write, so no "stop", no "0 A", nothing touches the current the user set.
            # Deliberately skips _finalize(): its enable/disable-delay logic would flip
            # `charge` back on, and clamping is meaningless without a target.
            d.manual = True
            d.charge = False
            d.target_current = 0.0
            d.blocked_by = ""
            d.reason = "manual mode: app is not controlling the charger"
            return d

        if s.mode == MODE_OFF:
            d.charge = False
            d.reason = "mode off"
            return self._finalize(d, charger, now)

        if s.mode == MODE_NOW:
            d.charge = charger.connected
            d.target_current = hi
            d.reason = "mode now: charge at max current"
            return self._finalize(d, charger, now)

        if not charger.connected:
            self._known_phases = 0          # unplugged: the cable may be a different one
            d.charge = False
            d.reason = "no vehicle connected"
            return self._finalize(d, charger, now)

        # Staleness gate - it sits above every solar mode, because the reading it
        # guards is older than any of them can tolerate. The proxy answers from its
        # cache whenever the inverter does not reply (its Modbus target is off every
        # night), so an eleven-hour-old frame arrives here looking perfectly normal:
        # same shape, same units, no error. Deciding on it is how a car charges from
        # the grid all night on numbers from dusk. `now` mode is deliberately exempt:
        # an explicit forced charge does not need the meter, and it is the user's own
        # instruction. No grace either - with a blind meter the earlier the stop, the
        # less grid energy is wasted.
        if site_stale_s >= s.site_stale_s and not d.cheap_now:
            # (Not inside the cheap window: that charge is decided by the clock alone and
            # needs no site reading, so a frozen meter must not block it.)
            d.charge = False
            d.target_current = 0.0
            d.reason = ("site reading stale (held %.0f min) - not acting on it"
                        % (site_stale_s / 60.0))
            return d

        # ---- cheap_hours: the cheap tariff window, plus whatever sun turns up --
        # Named after the cheap_hours setting it uses, so the two are impossible to
        # confuse. Only this mode uses that window: pv and minpv stay pure, because
        # with enough sun configured there a night-time grid charge nobody asked for
        # is exactly the surprise this mode exists to prevent. Placed behind the
        # connection check so an empty driveway is reported as such, not as a charge.
        #
        # Inside the window this is *unconditional and continuous*: maximum current until
        # the window closes, no dwell timers, no re-limiting - and including a charge that
        # is already running. The previous version only *started* a charge (`and not
        # charger.charging`) and let a running one fall through to the surplus logic, which
        # re-limited it to the minimum and then stopped it after the off-grace, whereupon
        # the window started it again: an on/off cycle every ~5 minutes, 69 times in one
        # night. A car's on-board charger is not a switch, and repairing one costs
        # thousands - hence the safety counter in safety.py.
        if s.mode == MODE_CHEAP and d.cheap_now:
            d.target_current = hi
            d.charge = True
            d.reason = ("cheap_hours: cheap tariff window - charging from the grid at "
                        "max until the window closes")
            return self._finalize(d, charger, now)

        # plan support: reach a deadline with a known amount of energy. The rate needed
        # to finish in time is the floor; any surplus on top of it goes into the car as
        # well, so a deadline charge still uses the sun when there is one.
        plan_w = self._plan_required_w(session_kwh, now, d)
        if plan_w > 0:
            need_a = plan_w / (ph * s.voltage_nominal) if ph else 0.0
            if need_a < lo and not charger.charging:
                # Stretch: the deadline needs less than the car's minimum current, so
                # starting now would finish hours early on grid power nobody asked for.
                # Hold off until the required rate reaches the minimum; the web UI
                # counts that down.
                self._enable_since = None       # a deliberate wait, not an enable grace
                d.charge = False
                d.plan_wait = True
                d.enable_in_s = self._plan_start_in_s(plan_w, d.planned_kwh, lo, ph)
                d.reason = ("plan: %.1f kWh to go, %.0f W needed - under the %.1f A "
                            "minimum, holding off" % (d.planned_kwh, plan_w, lo))
            else:
                sun_a = surplus / (ph * s.voltage_nominal) if ph else 0.0
                d.plan_active = True
                d.target_current = max(lo, min(hi, max(need_a, sun_a)))
                d.charge = True
                d.reason = "plan: %.1f kWh to go, need %.0f W" % (d.planned_kwh, plan_w)
            return self._finalize(d, charger, now)

        if s.mode == MODE_MINPV:
            min_start = self._power_for_current(lo, ph)
            floor = self._floor_w(min_start, charger)
            d.available_w = round(surplus, 1)
            if ph and surplus >= floor:
                d.charge = True
                d.target_current = lo
                d.reason = "minpv: surplus %.0f W >= %.0f W (%dp)" % (surplus, floor, ph)
            else:
                d.charge = False
                d.reason = "minpv: surplus %.0f W below %.0f W (%dp)" % (surplus, floor, ph)
            return self._finalize(d, charger, now)

        # MODE_PV (and MODE_CHEAP outside its cheap window): the current follows the
        # surplus immediately - no ramp, no band. Inside 6..max A there is nothing to
        # smooth: the target *is* the surplus, so following it at once uses the sun
        # instead of trailing it. Hysteresis belongs only at the floor, and there it is
        # asymmetric (see _floor_w): the start demands the minimum plus the enable margin,
        # the hold only the minimum minus the disable margin. Below the floor the car
        # cannot be charged at all, so the decision says stop and _finalize's disable grace
        # holds the charge at the minimum instead - with the countdown visible in the web UI.
        tag = "cheap_hours" if s.mode == MODE_CHEAP else "pv"
        raw = surplus / (ph * s.voltage_nominal) if ph else 0.0
        min_start = self._power_for_current(lo, ph)
        floor = self._floor_w(min_start, charger)
        d.available_w = round(surplus, 1)

        if ph and surplus >= floor:
            d.charge = True
            # Inside the band the surplus alone is under the minimum, so clamp to it: the
            # car keeps charging at 6 A and the small deficit comes off the house battery,
            # which is the whole point of holding instead of stopping.
            d.target_current = max(lo, min(hi, raw))
            d.reason = "%s: surplus %.0f W -> %.1f A (%dp)" % (
                tag, surplus, d.target_current, ph)
        else:
            d.charge = False
            d.reason = "%s: surplus %.0f W below minimum (%.0f W, %dp)" % (tag, surplus, floor, ph)
        return self._finalize(d, charger, now)

    # -- plan ------------------------------------------------------------
    def _plan_required_w(self, session_kwh: float, now: datetime, d: Decision) -> float:
        s = self.s
        if s.plan_energy_kwh <= 0 or not s.plan_deadline:
            return 0.0
        deadline_time = _parse_hm(s.plan_deadline)
        if deadline_time is None:
            return 0.0
        deadline = now.replace(hour=deadline_time.hour, minute=deadline_time.minute,
                               second=0, microsecond=0)
        if deadline <= now:                      # deadline already passed today
            deadline = deadline + timedelta(days=1)
        remaining_h = max(0.1, (deadline - now).total_seconds() / 3600.0)
        to_go = max(0.0, s.plan_energy_kwh - session_kwh)
        d.planned_kwh = round(to_go, 2)
        if to_go <= 0:
            return 0.0
        return to_go * 1000.0 / remaining_h

    def _plan_start_in_s(self, plan_w: float, to_go_kwh: float, lo: float, ph: int) -> float:
        """Seconds until a stretched charge has to begin to be done by its deadline.

        The deadline needs `to_go_kwh`; charging at the minimum current can deliver
        `lo * phases * voltage` per hour, so the charge must start that many hours
        before the deadline. What is left of the window is the wait.
        """
        if plan_w <= 0 or to_go_kwh <= 0:
            return 0.0
        hours_left = to_go_kwh * 1000.0 / plan_w
        min_kw = lo * max(1, ph) * self.s.voltage_nominal / 1000.0
        need_hours = to_go_kwh / min_kw if min_kw > 0 else 0.0
        return max(0.0, (hours_left - need_hours) * 3600.0)

    # -- hysteresis / dwell timers ---------------------------------------
    def _finalize(self, d: Decision, charger: ChargerState, now: datetime) -> Decision:
        s = self.s
        t = now.timestamp()

        # Inside the cheap window the decision is unconditional: the owner asked for a grid
        # charge until the window closes, so the dwell timers, the battery block and the
        # current clamp have nothing to contribute - and any of them interrupting here is
        # exactly what produced a night of on/off cycling. Leave no dwell state behind, so
        # the transition out of the window starts clean and the normal grace applies then.
        if d.cheap_now:
            self._enable_since = None
            self._disable_since = None
            d.blocked_by = None
            lo, hi = self._currents_bounds()
            d.target_current = round(max(0.0, min(hi, d.target_current)), 2)
            d.charge = bool(d.charge and d.target_current > 0)
            return d

        # Dwell timers are keyed on the box's *permission* (alw -> enabled), not on the car's
        # actual draw (car==2 -> charging) - the same quantity hardware_actions compares
        # against. Keying them on `charging` produced the stops the owner saw on 20.09.2026:
        # a plugged car that is not drawing (full, or on a departure timer) made
        # `d.charge and not charger.charging` true in *every* cycle, so the enable grace
        # re-armed every cycle, the decision was forced to charge=False, and the write path -
        # which compares against `enabled` - dutifully switched the box off. One stop per
        # minute with plenty of surplus, five of them inside the safety window.
        if d.charge and not charger.enabled:
            self._enable_since = self._enable_since or t
        else:
            self._enable_since = None
        if not d.charge and charger.enabled:
            self._disable_since = self._disable_since or t
        else:
            self._disable_since = None

        # The remaining grace time is published on the decision so the web UI can show a
        # countdown ("stopping charging in 2 min if it doesn't get better") instead of a
        # bare reason string. While a grace runs the decision keeps the *opposite* of what
        # it is waiting for, and because the timers now follow `enabled`, that no longer
        # produces a write: `stopping` is only true when the box is really enabled.
        if not charger.enabled:
            if self._enable_since and (t - self._enable_since) < s.enable_delay_s:
                d.charge = False
                d.enable_in_s = round(s.enable_delay_s - (t - self._enable_since), 1)
                d.reason += " (waiting enable delay %ds)" % s.enable_delay_s
        else:
            if self._disable_since and (t - self._disable_since) < s.disable_delay_s:
                d.charge = True
                d.disable_in_s = round(s.disable_delay_s - (t - self._disable_since), 1)
                d.reason += " (waiting disable delay %ds)" % s.disable_delay_s

        # The house battery is only our business when we are charging from *surplus*.
        # Inside the cheap window the grid is paying, so a low battery must not block the
        # charge nor even be reported as blocking it. pv and minpv are surplus decisions,
        # and so is cheap_hours *outside* its window - that path falls through to the same
        # surplus logic, so it honours the guard too. `now` and `manual` stay untouched: a
        # forced charge is the owner's explicit instruction, battery or no battery.
        surplus_decision = (s.mode in (MODE_PV, MODE_MINPV)
                            or (s.mode == MODE_CHEAP and not d.cheap_now))
        lo, hi = self._currents_bounds()
        if d.blocked_by and surplus_decision and not d.plan_active:
            d.charge = False
            # blocked_by stays its own field: the API exposes it and the UI renders it
            # as a separate row. Appending it to `reason` produced one run-on line
            # ("no vehicle connected | blocked: battery below buffer SOC ...").

        # Above buffer SOC the house battery may carry a *running* charge: the reserve above
        # the buffer is there for exactly this, so pv/minpv holds the car at the minimum
        # current instead of switching it off the moment the sun goes (the reserve may
        # carry a running charge and merely delays switching off). It ends
        # when the SOC falls back to the buffer - from there the normal rules decide, i.e.
        # the charge stops. Starting a charge on battery energy is NOT this rule: only a
        # charge that is already running is held.
        if (surplus_decision and charger.charging and not d.plan_active and not d.blocked_by
                and not d.plan_wait and d.soc is not None and d.soc > s.buffer_soc
                and not d.charge and d.target_current < lo):
            d.charge = True
            d.target_current = lo
            d.enable_in_s = 0.0
            d.disable_in_s = 0.0
            d.reason += " (battery above buffer SOC: held at the minimum)"

        d.target_current = round(max(0.0, min(hi, d.target_current)), 2)
        if d.charge and d.target_current < lo:
            d.target_current = lo

        # No step limiting for tracking the sun: inside 6..max A the target *is* the
        # surplus, so a ramp only makes the car trail it. Below the minimum the grace
        # timer decides, not a ramp.

        d.granted_w = round(self._power_for_current(d.target_current), 1)
        self._last_decision = d
        return d

    def hardware_actions(self, d: Decision, charger: ChargerState,
                         site_stale_s: float = 0.0) -> List[str]:
        """Translate a decision into concrete charger writes. Never called in dry-run.

        Returns [] unconditionally in manual mode: the app must not start, stop or
        re-limit a charge the user is running by hand.

        Order matters: when starting a charge the current limit is written first,
        so the vehicle never starts drawing at whatever limit was left behind.
        """
        s = self.s
        actions: List[str] = []
        # Manual mode: zero writes, and this check sits above everything else (including
        # the control_enabled gate) so no code path can add one - and so switching to
        # manual while a car is charging sends no stop command.
        if s.mode == MODE_MANUAL or d.manual:
            return actions
        if not s.control_enabled:
            return actions
        # Blind-meter rule, and it is asymmetric on purpose: never start and never
        # re-limit, but a stop is always allowed - ending a charge we can no longer
        # justify is the conservative action, and leaving one running on stale numbers
        # is exactly the failure this guards against. (A start cannot hide here either:
        # the stale decision comes back with charge=False, so only alw=0 can be emitted.)
        if site_stale_s >= s.site_stale_s and not d.cheap_now:
            # (Inside the cheap window the charge is decided by the clock alone, so a
            # frozen meter neither blocks a start nor forces a stop.)
            return ["alw=0"] if (not d.charge and charger.enabled) else []
        t = time.time()
        # Start/stop edges compare against the box's *permission* (alw -> enabled), not
        # the car's actual draw (car==2 -> charging). A plugged car that is full or on a
        # departure schedule stays "not charging" indefinitely; comparing against
        # `charging` would then re-send the start command every single cycle.
        starting = d.charge and not charger.enabled
        stopping = (not d.charge) and charger.enabled

        if d.charge and d.target_current and abs(d.target_current - charger.max_current) >= 0.5:
            if starting or (t - self._last_change) >= 5:
                actions.append("amx=%d" % int(round(d.target_current)))

        if starting or stopping:
            if starting or (t - self._last_change) >= s.min_change_interval_s:
                actions.append("alw=%d" % (1 if d.charge else 0))

        if actions:
            self._last_change = t
        return actions
