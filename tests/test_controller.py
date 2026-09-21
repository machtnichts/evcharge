#!/usr/bin/env python3
"""Decision-logic tests: no hardware, deterministic SiteState/ChargerState."""
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.controller import (ChargingController, Settings, MODE_PV, MODE_MINPV, MODE_NOW,
                                MODE_OFF, MODE_MANUAL, MODE_CHEAP)
from evcharge.drivers.solaredge import SiteState
from evcharge.drivers.goe import ChargerState

FAILS = []
TOTAL = 0


def check(name, cond, detail=""):
    # The count is measured, not written down: a stale number in the summary hides a
    # check that was deleted or parked behind an early return.
    global TOTAL
    TOTAL += 1
    # str() on purpose: several checks hand a list of charger writes straight in as the
    # detail, and that is exactly the output one wants to read when it fails.
    print("  %-62s %s%s" % (name, "PASS" if cond else "FAIL",
                            (" - " + str(detail)) if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def site(pv=4000, grid=-1000, bat=-500, soc=90):
    s = SiteState()
    s.pv_power_w, s.grid_power_w = pv, grid
    s.battery_power_w, s.battery_soc = bat, soc
    return s


def car(connected=True, charging=False, power=0, max_current=0, enabled=None):
    c = ChargerState()
    c.connected, c.charging, c.power, c.max_current = connected, charging, power, max_current
    # The wallbox's permission (`enabled`, i.e. alw) follows the car's draw unless a test
    # says otherwise: current cannot flow while the box is not enabling. The controller
    # keys its dwell timers *and* its writes on `enabled` - the draw is only what the car
    # does with the permission - so a fixture with charging=True and enabled=False would
    # describe a state the hardware cannot produce.
    c.enabled = bool(charging) if enabled is None else enabled
    c.car_state = "charging" if charging else ("waiting" if connected else "idle")
    return c


def dec(ctrl, s, c, **kw):
    return ctrl.decide(s, c, now=datetime(2026, 9, 12, 14, 0), session_kwh=kw.get("session", 0.0))


print("mode off")
c = ChargingController(Settings(mode=MODE_OFF, enable_delay_s=0, disable_delay_s=0))
d = dec(c, site(), car(connected=True, charging=True, power=2000))
check("off never charges", d.charge is False and d.target_current == 0, d.reason)

print("mode now")
c = ChargingController(Settings(mode=MODE_NOW, min_current=6, max_current=14,
                               enable_delay_s=0, disable_delay_s=0))
d = dec(c, site(grid=+3000), car(connected=True))
check("now charges at max even while importing", d.charge and d.target_current == 14, d.reason)
c2 = ChargingController(Settings(mode=MODE_NOW, enable_delay_s=0))
d2 = dec(c2, site(), car(connected=False))
check("now does not charge with no vehicle", d2.charge is False, d2.reason)

print("mode pv: surplus tracking")
c = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14,
                               enable_delay_s=0, disable_delay_s=0, buffer_soc=80))
d = dec(c, site(grid=-3000, bat=0, soc=95), car(connected=True))
check("3 kW surplus -> ~13 A", d.charge and 12.5 <= d.target_current <= 13.5,
      "%.2f A / %s" % (d.target_current, d.reason))
d = dec(c, site(grid=-1500, bat=0, soc=95), car(connected=True))
check("1.5 kW surplus -> ~6.5 A", d.charge and 6 <= d.target_current <= 7,
      "%.2f A" % d.target_current)
d = dec(c, site(grid=-800, bat=0, soc=95), car(connected=True))
check("800 W surplus -> below minimum -> no charge", d.charge is False, d.reason)
d = dec(c, site(grid=-1000, bat=0, soc=95), car(connected=True, charging=True, power=1380, max_current=6))
check("car draw counts towards surplus", d.charge and d.target_current > 6, d.reason)

print("battery bands: priority SOC hands over the intake, buffer SOC carries the car")
# Band 1 - below priority SOC: what the battery is *taking* stays with it. The car is not
# blocked; it charges from the real export, in which the battery's draw is already missing.
c = ChargingController(Settings(mode=MODE_PV, enable_delay_s=0, disable_delay_s=0,
                                buffer_soc=80, priority_soc=55))
d = dec(c, site(grid=-3000, bat=-800, soc=50), car(connected=True))
check("below priority SOC the car charges on the real export, not blocked",
      d.charge is True and not d.blocked_by and d.surplus_w == 3000.0,
      "%.0f W blocked=%r" % (d.surplus_w, d.blocked_by))
d = dec(c, site(grid=-1500, bat=-600, soc=54.9), car(connected=True))
check("just under priority SOC it is still the battery's share", d.surplus_w == 1500.0,
      "%.0f W" % d.surplus_w)
# Band 2 - from priority SOC up: the intake is offered to the car, so it outranks the
# battery's charging instead of waiting for it to finish.
d = dec(c, site(grid=-1500, bat=-600, soc=55.0), car(connected=True))
check("at priority SOC the car gets it: 1.5 kW + 600 W = 2.1 kW -> 9.1 A",
      d.surplus_w == 2100.0 and abs(d.target_current - 9.1) < 0.1,
      "%.0f W -> %.1f A" % (d.surplus_w, d.target_current))
d = dec(c, site(grid=-3000, bat=-800, soc=95), car(connected=True))
check("and higher up the same: 3 kW + 800 W = 3.8 kW -> capped at 14 A",
      d.surplus_w == 3800.0 and d.target_current == 14.0,
      "%.0f W -> %.1f A" % (d.surplus_w, d.target_current))
# Band 3 - above buffer SOC the reserve above the buffer carries a *running* charge, so
# pv/minpv holds the car at the minimum instead of switching it off (the reserve may
# carry a running charge and merely delays switching off). It ends at the buffer.
c_carry = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                      enable_delay_s=0, disable_delay_s=0, buffer_soc=80,
                                      priority_soc=55))
run = car(connected=True, charging=True, power=1380, max_current=6)
run.phases = 1
d = dec(c_carry, site(pv=0, grid=+400, bat=+1500, soc=90), run)
check("above buffer SOC a running charge is held at 6 A on the battery",
      d.charge is True and d.target_current == 6.0 and "held at the minimum" in d.reason,
      "%.1f A  %s" % (d.target_current, d.reason))
d = dec(c_carry, site(pv=0, grid=+400, bat=+1500, soc=80), run)
check("at the buffer that ride ends and the surplus rules stop the charge",
      d.charge is False and d.target_current == 0.0 and "held at the minimum" not in d.reason,
      d.reason)
d = dec(c_carry, site(pv=0, grid=+400, bat=+1500, soc=90), car(connected=True))
check("a charge that is not running is never started on battery energy",
      d.charge is False, d.reason)
d = dec(c_carry, site(grid=-1500, bat=+800, soc=95), car(connected=True))
check("a discharging battery is never handed to the car (700 W -> no charge)",
      d.surplus_w == 700.0 and d.charge is False, "%.0f W" % d.surplus_w)
check("residual_power_w defaults to 0: the meter already measures the house",
      Settings().residual_power_w == 0.0, "%.0f W" % Settings().residual_power_w)

print("the grey-day case the owner asked for")
c_band = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                    enable_delay_s=0, disable_delay_s=0, buffer_soc=80,
                                    priority_soc=55))
d_band = dec(c_band, site(pv=2000, grid=-3000, bat=-1500, soc=50), car(connected=True))
check("grey day, battery half full: the car charges on the real 3 kW export",
      d_band.charge is True and d_band.surplus_w == 3000.0 and not d_band.blocked_by,
      "%.0f W -> %.1f A" % (d_band.surplus_w, d_band.target_current))
d_band = dec(c_band, site(pv=2000, grid=+100, bat=-1500, soc=50), car(connected=True))
check("and with nothing left after the battery it waits",
      d_band.charge is False and d_band.surplus_w == -100.0, d_band.reason)

# Below priority SOC the battery's reserve is not the car's: while it is supplying the
# car and nothing is exported, the charge stops instead of draining it further.
c_pr = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                  enable_delay_s=0, disable_delay_s=180, buffer_soc=80,
                                  priority_soc=55))
live = car(connected=True, charging=True, power=3000, max_current=14)
live.phases = 1
d2 = dec(c_pr, site(grid=0, bat=+800, soc=40), live)
check("battery at/below priority SOC + no export -> the guard stops the drainage",
      d2.charge is False and "priority" in (d2.blocked_by or ""), "blocked_by=%r" % d2.blocked_by)
d2 = dec(c_pr, site(grid=0, bat=+800, soc=60), live)
check("above priority SOC the same charge runs on", d2.charge is True, d2.reason)


print("mode minpv")
c = ChargingController(Settings(mode=MODE_MINPV, min_current=6, max_current=14,
                               enable_delay_s=0, disable_delay_s=0, buffer_soc=80))
d = dec(c, site(grid=-1400, bat=0, soc=95), car(connected=True))
check("minpv charges at min current on 1.4 kW", d.charge and d.target_current == 6, d.reason)
d = dec(c, site(grid=-900, bat=0, soc=95), car(connected=True))
check("minpv stays off below minimum", d.charge is False, d.reason)

print("delays, and following the sun at once")
c = ChargingController(Settings(mode=MODE_PV, enable_delay_s=60, disable_delay_s=0, buffer_soc=80))
d = dec(c, site(grid=-3000, bat=0, soc=95), car(connected=True))
check("enable delay suppresses immediate start", d.charge is False and "delay" in d.reason, d.reason)
c = ChargingController(Settings(mode=MODE_PV, enable_delay_s=0, disable_delay_s=0, buffer_soc=80))
d = dec(c, site(grid=-1000, bat=0, soc=95), car(connected=True, charging=True, power=1300, max_current=6))
check("inside 6..max the current goes straight to the surplus (no ramp)",
      abs(d.target_current - 10.0) < 0.1 and "ramped" not in d.reason,
      "%.2f A (box was at 6 A) %s" % (d.target_current, d.reason))

print("cheap tariff window: mode cheap_hours only")
night = datetime(2026, 9, 12, 2, 0)
c = ChargingController(Settings(mode=MODE_PV, cheap_hours=["00:00-05:00"], enable_delay_s=0))
d = c.decide(site(grid=+200, soc=95), car(connected=True), now=night)
check("pv mode ignores the cheap window (no grid charge in the sun season)",
      d.charge is False and d.cheap_now is True, d.reason)
c = ChargingController(Settings(mode=MODE_MINPV, cheap_hours=["00:00-05:00"], enable_delay_s=0))
d = c.decide(site(grid=+200, soc=95), car(connected=True), now=night)
check("minpv mode ignores the cheap window too", d.charge is False and d.cheap_now is True, d.reason)
w = ChargingController(Settings(mode=MODE_CHEAP, cheap_hours=["00:00-05:00"], min_current=6,
                                max_current=14, enable_delay_s=0, disable_delay_s=0))
d = w.decide(site(grid=+200, soc=95), car(connected=True), now=night)
check("cheap_hours mode in the window charges from the grid at max",
      d.charge is True and d.target_current == 14 and "cheap tariff" in d.reason, d.reason)

print("the battery is our business only when charging from surplus")
w2 = ChargingController(Settings(mode=MODE_CHEAP, cheap_hours=["00:00-05:00"], min_current=6,
                                 max_current=14, enable_delay_s=0, disable_delay_s=0,
                                 buffer_soc=95))
d = w2.decide(site(grid=+200, soc=40), car(connected=True), now=night)
check("in the window a low battery neither blocks nor is reported",
      d.charge is True and not d.blocked_by, "%s blocked=%r" % (d.reason, d.blocked_by))
w3 = ChargingController(Settings(mode=MODE_CHEAP, cheap_hours=["00:00-05:00"], min_current=6,
                                 max_current=14, enable_delay_s=0, disable_delay_s=0,
                                 buffer_soc=95))
d = w3.decide(site(grid=-2000, soc=40), car(connected=True),
              now=datetime(2026, 9, 12, 14, 0))
check("outside the window it is a surplus charge on the real export",
      d.charge is True and d.surplus_w == 2000.0,
      "%.0f W blocked=%r" % (d.surplus_w, d.blocked_by))
d = w3.decide(site(grid=-2000, soc=95), car(connected=True),
              now=datetime(2026, 9, 12, 14, 0))
check("and above the buffer the battery's 500 W intake is handed over",
      d.charge is True and d.surplus_w == 2500.0 and not d.blocked_by,
      "%.0f W" % d.surplus_w)

print("inside the cheap window the charge runs continuously at maximum")
# This is the bug that switched a car 69 times in one night: a charge already running
# fell through to the surplus logic, was re-limited to the minimum and then stopped,
# whereupon the window started it again. A running charge must stay at maximum.
w4 = ChargingController(Settings(mode=MODE_CHEAP, cheap_hours=["00:00-05:00"], min_current=6,
                                 max_current=14, enable_delay_s=60, disable_delay_s=180,
                                 buffer_soc=80))
running = car(connected=True, charging=True, power=9660)
running.enabled = True
running.max_current = 14
d = w4.decide(site(grid=+500, soc=40), running, now=night)
check("a running charge in the window stays at maximum",
      d.charge is True and d.target_current == 14.0,
      "%s -> %s A" % (d.reason, d.target_current))
check("no dwell timer or battery block interrupts it",
      not d.blocked_by and d.disable_in_s == 0.0,
      "blocked=%r disable_in_s=%s" % (d.blocked_by, d.disable_in_s))
acts = w4.hardware_actions(d, running)
check("and it emits no on/off edge", [a for a in acts if a.startswith("alw=")] == [], acts)

print("at the window's end it hands over to the PV rules - no stop, no restart")
w5 = ChargingController(Settings(mode=MODE_CHEAP, cheap_hours=["00:00-05:00"], min_current=6,
                                 max_current=14, enable_delay_s=0, disable_delay_s=180))
day = datetime(2026, 9, 12, 12, 0)               # outside the window
still = car(connected=True, charging=True, power=9660)
still.enabled = True
still.max_current = 14
d = w5.decide(site(grid=-3000, soc=95), still, now=day)
check("surplus after the window keeps the same charge running",
      d.charge is True and d.target_current > 6.0, d.reason)
acts2 = w5.hardware_actions(d, still)
check("with no on/off edge - a seamless handover",
      [a for a in acts2 if a.startswith("alw=")] == [], acts2)
d2 = w5.decide(site(grid=+9660, soc=95), still, now=day)
check("and no surplus after the window ends it, under the normal grace",
      d2.disable_in_s > 0 and "disable" in d2.reason, d2.reason)

print("a frozen meter does not block the window charge")
w6 = ChargingController(Settings(mode=MODE_CHEAP, cheap_hours=["00:00-05:00"], min_current=6,
                                 max_current=14, enable_delay_s=60, disable_delay_s=180))
d = w6.decide(site(grid=0, soc=50), car(connected=True), now=night, site_stale_s=99999.0)
check("the window proceeds on the clock alone",
      d.charge is True and d.target_current == 14.0, d.reason)
d = w.decide(site(grid=-3000, bat=0, soc=95), car(connected=True), now=datetime(2026, 9, 12, 14, 0))
check("cheap_hours mode outside the window follows the sun like pv",
      d.charge is True and abs(d.target_current - 13.0) < 0.2 and d.reason.startswith("cheap_hours"),
      "%.1f A  %s" % (d.target_current, d.reason))
d = w.decide(site(grid=-3000, soc=95), car(connected=False), now=night)
check("cheap_hours mode does not charge with no vehicle",
      d.charge is False and "no vehicle" in d.reason, d.reason)

print("plan: charge 10 kWh by a deadline")
c = ChargingController(Settings(mode=MODE_PV, plan_energy_kwh=10, plan_deadline="18:00",
                               enable_delay_s=0, disable_delay_s=0, buffer_soc=80))
d = c.decide(site(grid=-100, soc=95), car(connected=True), now=datetime(2026, 9, 12, 14, 0),
             session_kwh=0)
check("plan overrides solar shortfall", d.charge and d.plan_active, d.reason)
d = c.decide(site(grid=-100, soc=95), car(connected=True), now=datetime(2026, 9, 12, 14, 0),
             session_kwh=10)
check("plan inactive once energy delivered", d.plan_active is False, d.reason)

print("safety: hardware actions require control_enabled")
c = ChargingController(Settings(mode=MODE_PV, control_enabled=False, enable_delay_s=0))
d = dec(c, site(grid=-3000, soc=95), car(connected=True))
check("dry run produces no actions", c.hardware_actions(d, car(connected=True)) == [])
c = ChargingController(Settings(mode=MODE_PV, control_enabled=True, enable_delay_s=0,
                               min_change_interval_s=0))
acts = c.hardware_actions(dec(c, site(grid=-3000, soc=95), car(connected=True, max_current=6)),
                          car(connected=True, max_current=6))
check("control enabled produces charger commands", any(a.startswith("amx=") for a in acts), str(acts))

print("manual mode: the charger belongs to the user")
c = ChargingController(Settings(mode=MODE_MANUAL, control_enabled=True,
                                enable_delay_s=0, disable_delay_s=0, min_change_interval_s=0))
cs = car(connected=True, charging=True, power=2300, max_current=10)
d = dec(c, site(grid=-3000, soc=95), cs)
check("manual: app decides nothing", d.manual is True and d.charge is False, d.reason)
check("manual: no target current", d.target_current == 0.0, str(d.target_current))
check("manual: battery rules are not reported as blockers", d.blocked_by == "", d.blocked_by)
check("manual: an ongoing manual charge is not stopped", c.hardware_actions(d, cs) == [],
      str(c.hardware_actions(d, cs)))
cs2 = car(connected=True)
d2 = dec(c, site(grid=-3000, soc=95), cs2)
check("manual: full surplus yields no commands", c.hardware_actions(d2, cs2) == [],
      str(c.hardware_actions(d2, cs2)))
c3 = ChargingController(Settings(mode=MODE_PV, control_enabled=True, enable_delay_s=0,
                                 min_change_interval_s=0))
d3 = dec(c3, site(grid=-3000, soc=95), cs2)
check("leaving manual resumes control", len(c3.hardware_actions(d3, cs2)) > 0,
      str(c3.hardware_actions(d3, cs2)))

print("phases: assume the cable (1p) to start, follow the box once current flows")
# phases only matter for the surplus maths now that the current follows it directly
# (disable grace on: a 3-phase car below the floor is *held* at 6 A, not dropped)
c = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                max_current_step=14, enable_delay_s=0, disable_delay_s=180,
                                buffer_soc=80))
d = dec(c, site(grid=-1500, soc=95), car(connected=True))
check("1-phase car starts at 1.5 kW (not at the 3-phase floor)", d.charge is True, d.reason)
cs = car(connected=True)
cs.phases = 3            # supply phases only; nothing flows yet
d = dec(c, site(grid=-1500, soc=95), cs)
check("box's supply phases ignored before charging", d.charge is True and "(1p)" in d.reason, d.reason)
cs1 = car(connected=True, charging=True, max_current=6)
cs1.phases = 1
d1 = dec(c, site(grid=-3000, soc=95), cs1)
check("measured 1 phase -> ~13 A from 3 kW", d1.charge and d1.target_current >= 12.5,
      "%.2f A %s" % (d1.target_current, d1.reason))
cs3 = car(connected=True, charging=True, max_current=6)
cs3.phases = 3
d3 = dec(c, site(grid=-3000, soc=95), cs3)
check("measured 3 phases -> held at minimum, not 13 A", d3.charge and d3.target_current <= 6.1,
      "%.2f A %s" % (d3.target_current, d3.reason))

print("hysteresis: a passing cloud or the kettle must not switch the car off")
h = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                enable_delay_s=60, disable_delay_s=180, buffer_soc=80))
cs = car(connected=True, charging=True, power=1380, max_current=6)
t0 = datetime(2026, 9, 12, 14, 0)
d = h.decide(site(grid=+800, soc=95), cs, now=t0, session_kwh=0)
check("surplus collapses -> charge continues, timer starts", d.charge is True, d.reason)
d = h.decide(site(grid=-3000, soc=95), cs, now=datetime(2026, 9, 12, 14, 2), session_kwh=0)
check("kettle finished after 2 min -> never stopped", d.charge is True and "waiting disable" not in d.reason,
      d.reason)
h2 = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                 enable_delay_s=60, disable_delay_s=180, buffer_soc=80))
d = h2.decide(site(grid=+800, bat=0, soc=70), cs, now=t0, session_kwh=0)
d = h2.decide(site(grid=+800, bat=0, soc=70), cs, now=datetime(2026, 9, 12, 14, 4), session_kwh=0)
check("no surplus for 4 min -> stops after the grace period", d.charge is False, d.reason)
h3 = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                 enable_delay_s=60, disable_delay_s=180, buffer_soc=80))
idle = car(connected=True)
d = h3.decide(site(grid=-3000, soc=95), idle, now=t0, session_kwh=0)
check("surplus appears -> enable delay holds the start", d.charge is False and "enable delay" in d.reason,
      d.reason)
d = h3.decide(site(grid=+800, soc=95), idle, now=datetime(2026, 9, 12, 14, 1), session_kwh=0)
d = h3.decide(site(grid=-3000, soc=95), idle, now=datetime(2026, 9, 12, 14, 2), session_kwh=0)
check("surplus back within the enable delay -> timer restarts, no charge", d.charge is False, d.reason)

print("control edges use the box's permission (alw), not the car's draw")
c = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                control_enabled=True, enable_delay_s=0, disable_delay_s=0,
                                min_change_interval_s=0, max_current_step=14, buffer_soc=80))
allowed = car(connected=True, max_current=13)
allowed.enabled = True          # alw=1: the box may charge, even if the car is not drawing
allowed.phases = 1
d = dec(c, site(grid=-3000, bat=0, soc=95), allowed)
check("box already allowed + charge wanted -> no repeated start write",
      c.hardware_actions(d, allowed) == [], str(c.hardware_actions(d, allowed)))
blocked = car(connected=True, max_current=13)
blocked.enabled = True          # alw=1: the box is still allowing charging
d = dec(c, site(grid=+1500, soc=95), blocked)
check("charge no longer wanted but box still allows -> alw=0",
      "alw=0" in c.hardware_actions(d, blocked), str(c.hardware_actions(d, blocked)))

print("a dip drops straight to the minimum instead of crawling for minutes")
c = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                max_current_step=1, enable_delay_s=0, disable_delay_s=180,
                                buffer_soc=80))
cs_fast = car(connected=True, charging=True, power=3200, max_current=14)
cs_fast.phases = 1
d_dip = dec(c, site(pv=1000, grid=2300, bat=0, soc=100), cs_fast)   # ~900 W left
check("below minimum while charging -> held at the minimum, no ramp",
      d_dip.charge and d_dip.target_current == 6.0 and "ramped" not in d_dip.reason,
      "%.1f A  %s" % (d_dip.target_current, d_dip.reason))
cs_slow = car(connected=True, charging=True, power=600, max_current=6)
cs_slow.phases = 1
d_up = dec(c, site(pv=4000, grid=-2500, bat=0, soc=100), cs_slow)
check("a returning surplus jumps straight to the sun, no ramp",
      abs(d_up.target_current - 13.5) < 0.1 and "ramped" not in d_up.reason,
      "%.1f A (box was at 6 A)  %s" % (d_up.target_current, d_up.reason))

print("the house battery must not be drained into the car")
c = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                max_current_step=14, enable_delay_s=0, disable_delay_s=0,
                                buffer_soc=80))
cs_live = car(connected=True, charging=True, power=3190, max_current=14)
cs_live.phases = 1
d_flat = dec(c, site(grid=-13, bat=0, soc=100), cs_live)
d_drain = dec(c, site(grid=-13, bat=169, soc=100), cs_live)
check("battery discharge lowers the target by its own share",
      d_flat.target_current - d_drain.target_current >= 0.6,
      "%.2f A with a quiet battery vs %.2f A with 169 W discharge"
      % (d_flat.target_current, d_drain.target_current))
# The two rules live next to each other: below buffer SOC the battery's intake stays out
# of the surplus (the meter already carries the battery's draw - counting it again would
# hold the charge low), above it the intake is handed to the car on purpose, so the
# battery is held at its level instead of creeping on to 100 %.
c_q = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                 enable_delay_s=0, disable_delay_s=0, buffer_soc=80))
idling = car(connected=True, charging=True, power=0, max_current=14)
idling.phases = 1
below_flat = dec(c_q, site(grid=-2000, bat=0, soc=54), idling)
below_chg = dec(c_q, site(grid=-2000, bat=-300, soc=54), idling)
check("below priority SOC a charging battery is not double counted",
      below_flat.surplus_w == below_chg.surplus_w == 2000.0 and
      abs(below_flat.target_current - below_chg.target_current) < 0.01,
      "%.0f W / %.2f A with and without a 300 W battery charge"
      % (below_chg.surplus_w, below_chg.target_current))
above_flat = dec(c_q, site(grid=-2000, bat=0, soc=100), idling)
above_chg = dec(c_q, site(grid=-2000, bat=-300, soc=100), idling)
check("from priority SOC up its 300 W intake is handed to the car (1.3 A more)",
      above_chg.surplus_w == 2300.0 and
      abs((above_chg.target_current - above_flat.target_current) - 300.0 / 230.0) < 0.02,
      "%.0f W -> %.2f A vs %.2f A" % (above_chg.surplus_w, above_chg.target_current,
                                     above_flat.target_current))

print("a deadline charge with time to spare waits instead of finishing early")
c = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                enable_delay_s=60, disable_delay_s=180, buffer_soc=80,
                                plan_energy_kwh=2.0, plan_deadline="18:00"))
pm = datetime(2026, 9, 12, 14, 0)
d = c.decide(site(grid=-100, bat=0, soc=100), car(connected=True), now=pm, session_kwh=0.0)
check("2 kWh by 18:00 is under 6 A -> hold off, do not finish hours early",
      d.charge is False and d.plan_wait and "holding off" in d.reason, d.reason)
check("the wait is published for the countdown (~2.5 h of it)",
      abs(d.enable_in_s - 9183) < 120, "%.0f s" % d.enable_in_s)

print("a deadline that needs the minimum or more charges now - on the sun if offered")
c = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                enable_delay_s=0, disable_delay_s=180, buffer_soc=80,
                                plan_energy_kwh=6.0, plan_deadline="18:00"))
d = c.decide(site(grid=-3000, bat=0, soc=100), car(connected=True), now=pm, session_kwh=0.0)
check("6 kWh by 18:00 needs 1500 W, 3 kW of sun is on offer -> follow the sun",
      d.charge and d.plan_active and abs(d.target_current - 13.0) < 0.2,
      "%.1f A  %s" % (d.target_current, d.reason))

print("phases are remembered while the car stays plugged in")
c = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                enable_delay_s=0, disable_delay_s=0, buffer_soc=80))
cs3p = car(connected=True, charging=True, power=4000, max_current=6)
cs3p.phases = 3
c.decide(site(grid=-3000, bat=0, soc=100), cs3p, now=pm, session_kwh=0.0)
cs_idle = car(connected=True)          # same cable, charge stopped, nothing flowing
d = c.decide(site(grid=-3000, bat=0, soc=100), cs_idle, now=pm, session_kwh=0.0)
check("a 3-phase charge is remembered for the next decision", "3p)" in d.reason, d.reason)
unplugged = car(connected=False)
c.decide(site(grid=-3000, bat=0, soc=100), unplugged, now=pm, session_kwh=0.0)
d = c.decide(site(grid=-3000, bat=0, soc=100), cs_idle, now=pm, session_kwh=0.0)
check("unplugging drops the memory back to the configured 1 phase", "(1p)" in d.reason, d.reason)

print("the grace countdown is published for the web UI")
c = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                max_current_step=1, enable_delay_s=60, disable_delay_s=180,
                                buffer_soc=80))
cs_g = car(connected=True, charging=True, power=2000, max_current=14)
cs_g.phases = 1
dip_site = site(pv=0, grid=2500, bat=0, soc=100)
g1 = c.decide(dip_site, cs_g, now=datetime(2026, 9, 12, 14, 0), session_kwh=0.0)
g2 = c.decide(dip_site, cs_g, now=datetime(2026, 9, 12, 14, 1), session_kwh=0.0)
check("disable grace publishes the seconds left, counting down",
      g1.disable_in_s == 180.0 and g2.disable_in_s == 120.0,
      "%.0f s then %.0f s" % (g1.disable_in_s, g2.disable_in_s))
check("the countdown runs only while the charge is held",
      g1.charge and g2.charge and g1.enable_in_s == 0.0 and g1.target_current == 6.0,
      "charge=%s/%s target=%.1f A" % (g1.charge, g2.charge, g1.target_current))

print("a stale site reading is never acted on (the proxy cache trap)")
# The real incident: at night the inverter's Modbus target stops answering, the proxy
# keeps replying from its cache, and an eleven-hour-old frame arrives at decide()
# looking perfectly normal - same shape, same units, no error, always "not charging"
# by luck of the last evening's numbers.
c = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                enable_delay_s=0, disable_delay_s=0, buffer_soc=80,
                                control_enabled=True, min_change_interval_s=0,
                                site_stale_s=600))
stale_h = 11 * 3600.0
d = c.decide(site(grid=-3000, soc=95), car(connected=True), now=pm, session_kwh=0.0,
             site_stale_s=stale_h)
check("11 h old reading -> no charge, and the reason says why",
      d.charge is False and "stale" in d.reason, d.reason)
check("a blind meter never starts a charge",
      c.hardware_actions(d, car(connected=True, max_current=6), site_stale_s=stale_h) == [],
      str(c.hardware_actions(d, car(connected=True, max_current=6), site_stale_s=stale_h)))
running = car(connected=True, charging=True, power=2000, max_current=14)
running.enabled = True
acts_stale = c.hardware_actions(d, running, site_stale_s=stale_h)
check("a blind meter still stops a running charge - and never re-limits it",
      acts_stale == ["alw=0"], str(acts_stale))
d = c.decide(site(grid=-3000, soc=95), car(connected=True), now=pm, session_kwh=0.0,
             site_stale_s=300.0)
check("5 min of staleness is inside the tolerance -> normal sun tracking",
      d.charge and d.target_current > 12.0, d.reason)

print("a plugged car that is not drawing must not be stopped every cycle")
# The 20.09.2026 incident, in the owner's words: the app must not send alw=0 unnecessarily.
# The enable grace was keyed on the car's *draw*, so a wallbox that was enabled with a full
# (or departure-scheduled) car re-armed it in every single cycle: the decision was forced to
# charge=False, and the write path - which compares against the box's permission - switched
# the charge off. One stop per minute with 2 kW of surplus, five of them inside the safety
# window. Ten cycles now have to write nothing at all.
h = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                enable_delay_s=60, disable_delay_s=180, buffer_soc=80,
                                enable_threshold_w=300, disable_threshold_w=300,
                                control_enabled=True))   # writes need the explicit release
box_on_full = car(connected=True, charging=False, enabled=True, max_current=9)
writes, last = [], None
for i in range(10):
    last = h.decide(site(pv=4000, grid=-2000, bat=0, soc=95), box_on_full,
                    now=datetime(2026, 9, 12, 15, 0, 0) + timedelta(seconds=30 * i),
                    session_kwh=0.0)
    writes += h.hardware_actions(last, box_on_full)
check("ten cycles with a plugged, idle car write no switching at all",
      not any(w.startswith("alw=") for w in writes), writes)
check("...the decision keeps asking for a charge to be allowed",
      last.charge is True and last.target_current > 6.0, last.reason)
check("...and no stop grace is ever entered", "waiting disable" not in last.reason, last.reason)

print("the start grace waits on the wallbox and writes nothing while it waits")
h = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                enable_delay_s=60, disable_delay_s=180, buffer_soc=80,
                                enable_threshold_w=0, disable_threshold_w=0,
                                control_enabled=True))
box_off = car(connected=True, charging=False, enabled=False)
d1 = h.decide(site(pv=6000, grid=-2000, bat=0, soc=95), box_off,
              now=datetime(2026, 9, 12, 16, 0, 0), session_kwh=0.0)
w1 = h.hardware_actions(d1, box_off)
check("nothing at all is written while the start is held back", w1 == [], w1)
check("the decision reports the wait", d1.charge is False and "waiting enable delay" in d1.reason,
      d1.reason)
d2 = h.decide(site(pv=6000, grid=-2000, bat=0, soc=95), box_off,
              now=datetime(2026, 9, 12, 16, 1, 1), session_kwh=0.0)
w2 = h.hardware_actions(d2, box_off)
check("after the grace the current limit goes first, then the enable",
      len(w2) >= 2 and w2[0].startswith("amx=") and w2[-1] == "alw=1", w2)
check("no stop is written anywhere in between", "alw=0" not in w1 + w2, w1 + w2)

print("the stop grace holds first and writes exactly one stop afterwards")
h = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                enable_delay_s=0, disable_delay_s=180, buffer_soc=80,
                                enable_threshold_w=0, disable_threshold_w=0,
                                control_enabled=True))
drawing = car(connected=True, charging=True, power=1400, enabled=True, max_current=6)
drawing.phases = 1
# SOC below the buffer on purpose: above it the house battery deliberately *carries*
# the car at the minimum instead of stopping (its own rule), which would hide the grace.
d1 = h.decide(site(pv=0, grid=600, bat=0, soc=70), drawing,
              now=datetime(2026, 9, 12, 17, 0, 0), session_kwh=0.0)
w1 = h.hardware_actions(d1, drawing)
check("surplus 800 W while drawing: the charge is held, not cut",
      d1.charge is True and "waiting disable" in d1.reason, d1.reason)
check("...and nothing is written in the meantime", w1 == [], w1)
d2 = h.decide(site(pv=0, grid=600, bat=0, soc=70), drawing,
              now=datetime(2026, 9, 12, 17, 3, 1), session_kwh=0.0)
w2 = h.hardware_actions(d2, drawing)
check("after 180 s the decision stops - and exactly one alw=0 goes out",
      d2.charge is False and w2 == ["alw=0"], "%s | %s" % (d2.reason, w2))

print("the floor has 300 W of hysteresis, and it is asymmetric")
# One phase, 6 A minimum: the floor is 1380 W. Starting demands 1680 W, holding only
# 1080 W - so a surplus sitting on the floor neither opens nor ends a session, which is
# what stops the start/stop/start pattern the safety counter had to catch.
h = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                enable_delay_s=0, disable_delay_s=0, buffer_soc=80,
                                enable_threshold_w=300, disable_threshold_w=300))
box_off = car(connected=True, charging=False, enabled=False)
d = h.decide(site(pv=1400, grid=-1400, bat=0, soc=95), box_off,
             now=datetime(2026, 9, 12, 18, 0, 0), session_kwh=0.0)
check("1400 W is above the minimum but must not open a session (needs 1680 W)",
      d.charge is False, d.reason)
d = h.decide(site(pv=1700, grid=-1700, bat=0, soc=95), box_off,
             now=datetime(2026, 9, 12, 18, 1, 0), session_kwh=0.0)
check("1700 W does open one, at 7.4 A", d.charge is True and abs(d.target_current - 7.4) < 0.2,
      d.reason)
h2 = ChargingController(Settings(mode=MODE_PV, min_current=6, max_current=14, phases=1,
                                 enable_delay_s=0, disable_delay_s=0, buffer_soc=80,
                                 enable_threshold_w=300, disable_threshold_w=300))
holding = car(connected=True, charging=True, power=1380, enabled=True, max_current=6)
holding.phases = 1
d = h2.decide(site(pv=0, grid=100, bat=0, soc=70), holding,
              now=datetime(2026, 9, 12, 18, 2, 0), session_kwh=0.0)
check("1280 W does not end a running charge (the hold floor is 1080 W)",
      d.charge is True, d.reason)
check("...it holds at the minimum instead of dropping to zero",
      d.target_current == 6.0, "%.1f A" % d.target_current)
d = h2.decide(site(pv=0, grid=600, bat=0, soc=70), holding,
              now=datetime(2026, 9, 12, 18, 3, 0), session_kwh=0.0)
check("800 W is below the hold floor, so the decision stops", d.charge is False, d.reason)

print("\n%d passed, %d failed" % (TOTAL - len(FAILS), len(FAILS)))
if FAILS:
    print("failed:", FAILS)
sys.exit(1 if FAILS else 0)
