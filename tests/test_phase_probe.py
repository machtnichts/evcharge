#!/usr/bin/env python3
"""Phase policy: what the go-e actually reports, and how the count is assumed/measured.

Two things are pinned here:

1. The go-e's /status `nrg` layout, against a real sample captured while the car drew 6 A.
   The documented layout puts the currents at [3..5]; this box puts them at [4..6], and
   reading the documented indices counted a three-phase charge as two phases - while the
   go-e app showed 1.3 kW on every phase at that moment.
2. The owner's rule: after a plug-in, assume ONE phase, so the controller tries a charge
   at the 1-phase minimum (~1.4 kW) instead of parking the car until 3-phase surplus
   arrives. Current that actually flows settles it - but only once the charge has settled,
   because the opening cycles catch the phases coming up.
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.controller import (  # noqa: E402
    ChargingController, Settings, MODE_PV, RAMP_S,
)
from evcharge.drivers.goe import GoEClient, ChargerState  # noqa: E402
from evcharge.drivers.solaredge import SiteState  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  %-68s %s%s" % (name, "PASS" if cond else "FAIL",
                            (" - " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


# The raw array, unedited, captured 2026-09-18 ~18:16 while the app charged at 6 A: the
# go-e app showed 1.3 kW on each of three phases, and the car is a 3-phase car on a
# 3-phase cable.
CHARGING_NRG = [228, 226, 227, 1, 60, 59, 59, 13, 13, 13, 0, 411, 100, 100, 100, 0]
IDLE_NRG = [228, 228, 228, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]


def parse(nrg, car="2", alw="1", amp="6"):
    """Run the driver's parser over a canned /status payload (no HTTP)."""
    client = GoEClient("127.0.0.1")
    payload = json.dumps({"nrg": nrg, "car": car, "alw": alw, "amp": amp,
                          "fwv": "041.0", "eto": 200570})
    client._get = lambda path: payload          # test seam: no socket
    return client.status()


print("the go-e array, against a real sample (three phases at 1.3 kW each)")
st = parse(CHARGING_NRG)
check("voltages from [0..2]", st.voltages == [228.0, 226.0, 227.0], st.voltages)
check("currents from [4..6]: 6.0 / 5.9 / 5.9 A", st.currents == [6.0, 5.9, 5.9], st.currents)
check("three phases counted, not two", st.phases == 3, st.phases)
check("per-phase power from [7..9]: 1.3 kW each",
      st.power_per_phase == [1.3, 1.3, 1.3], st.power_per_phase)
check("total power from [11]: 4110 W", st.power == 4110.0, st.power)
check("power factor from [12]", st.power_factor == 1.0, st.power_factor)
check("the constant at nrg[3] is not read as a current", 0.1 not in st.currents, st.currents)

print("idle: the same constant is present, and no phase is reported")
st = parse(IDLE_NRG, car="3", alw="0")
check("no currents while idle", st.currents == [0.0, 0.0, 0.0], st.currents)
check("no phases while idle", st.phases == 0, st.phases)
check("plugged in but not charging", st.charging is False and st.connected is True)


def car(connected=True, charging=False, power=0, phases=0, max_current=6):
    c = ChargerState()
    c.connected, c.charging, c.power, c.phases = connected, charging, power, phases
    c.max_current = max_current
    c.car_state = "charging" if charging else ("waiting" if connected else "idle")
    return c


def site(grid=0, soc=95):
    # soc above buffer_soc: below it the controller blocks charging for the battery's sake
    s = SiteState()
    s.pv_power_w, s.grid_power_w = 0.0, grid
    s.battery_power_w, s.battery_soc = 0.0, soc
    return s


def mk(**kw):
    kw.setdefault("mode", MODE_PV)
    kw.setdefault("min_current", 6.0)
    kw.setdefault("max_current", 14.0)
    kw.setdefault("enable_delay_s", 0)
    kw.setdefault("disable_delay_s", 0)
    return ChargingController(Settings(**kw))


t0 = datetime(2026, 9, 18, 20, 0)

print("assume one phase until a charge has actually flowed")
c = mk()
check("nothing measured yet", c._known_phases == 0)
check("so the maths uses one phase", c._active_phases(ChargerState(), t0) == 1)
d = c.decide(site(grid=-1000), car(connected=True), now=t0)
check("a 1.0 kW surplus is below the 1-phase minimum: no charge",
      d.charge is False and d.phases == 1 and d.phases_checked is False,
      "%s | %sp" % (d.reason, d.phases))
d = c.decide(site(grid=-1380), car(connected=True), now=t0 + timedelta(seconds=35))
check("at the 1-phase minimum (~1.4 kW) it tries a charge",
      d.charge is True and d.target_current >= 6.0, d.reason)

print("the count is only believed once the charge has settled")
c = mk()
d = c.decide(site(grid=-3000), car(connected=True, charging=True, power=2760, phases=2),
             now=t0)
check("first cycle: still the assumed 1p (the phases are coming up)",
      d.phases == 1 and d.phases_checked is False,
      "%sp checked=%s" % (d.phases, d.phases_checked))
d = c.decide(site(grid=-3000), car(connected=True, charging=True, power=4140, phases=3),
             now=t0 + timedelta(seconds=int(RAMP_S) + 5))
check("after the ramp: three phases, checked",
      d.phases == 3 and d.phases_checked is True,
      "%sp checked=%s" % (d.phases, d.phases_checked))
d = c.decide(site(grid=-3000), car(connected=True, charging=True, power=1380, phases=1),
             now=t0 + timedelta(seconds=int(RAMP_S) + 35))
check("a later quiet phase never downgrades the memory", d.phases == 3, d.phases)

print("unplug forgets everything, so a cable change is caught")
d = c.decide(site(grid=0), car(connected=False), now=t0 + timedelta(seconds=90))
check("on unplug the memory is dropped", c._known_phases == 0)
check("and the assumption is one phase again",
      d.phases == 1 and d.phases_checked is False,
      "%sp checked=%s" % (d.phases, d.phases_checked))

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all phase-policy checks PASS")