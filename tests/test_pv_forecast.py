"""Tests for the PV forecast (step 1: display and logging only).

Two things are checked here: that the arithmetic is right, and that the forecast *cannot*
steer anything. The second is the promise step 1 makes to the owner ("erstmal sehen, wie es
so ist"), so it is pinned structurally - the module has no actuator vocabulary and the
controller never reads the forecast key.

No network: the fetch is injected.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.drivers.solaredge import SiteState                     # noqa: E402
from evcharge.main import Service, day_delta                            # noqa: E402
from evcharge.pv_forecast import (Forecast, Plane, PvForecast, build, factor, parse_hour,   # noqa: E402
                                  planes_kwp)

FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILS.append(name)
    print("  %-64s %s%s" % (name, "PASS" if cond else "FAIL", (" - " + detail) if detail else ""))


def series(day: str, hour_wh: dict, upto_hour: int = 23) -> dict:
    """A day of hourly GTI values: {hour: W/m2} - enough to exercise the sums."""
    times, vals = [], []
    for h in range(upto_hour + 1):
        times.append("%sT%02d:00" % (day, h))
        vals.append(float(hour_wh.get(h, 0.0)))
    return {"hourly": {"time": times, "global_tilted_irradiance": vals}}


def fake_fetcher(pages: dict, calls: list):
    def _f(url: str) -> dict:
        calls.append(url)
        for marker, answer in pages.items():
            if marker in url:
                return answer
        return {"hourly": {"time": [], "global_tilted_irradiance": []}}
    return _f


DAY = "2026-09-23"

print("the arithmetic: irradiance in, kilowatt hours out")
# 800 W/m2 on 8 kWp for one hour = 6400 Wh DC, x0.85 = 5440 Wh AC.
ans = build(series(DAY, {12: 800.0}), [Plane(8.0, -90, 25)], 0.85)
check("build(): GTI x kWp, per hour", abs(ans.dc_wh[DAY + "T12:00"] - 6400.0) < 0.01,
      str(ans.dc_wh[DAY + "T12:00"]))
check("build(): the performance ratio lands on the AC series",
      abs(ans.ac_wh[DAY + "T12:00"] - 5440.0) < 0.01, str(ans.ac_wh[DAY + "T12:00"]))
check("build(): the hour key keeps Open-Meteo's shape",
      parse_hour("2026-09-23T12:00") == "2026-09-23T12:00")
check("planes_kwp sums the array", abs(planes_kwp([Plane(4, -90), Plane(4, 90)]) - 8.0) < 1e-9)
try:
    build({"hourly": {"time": ["a"], "global_tilted_irradiance": []}}, [Plane(1, 0)], 1.0)
    check("build(): mismatched arrays raise instead of guessing", False, "no exception")
except ValueError:
    check("build(): mismatched arrays raise instead of guessing", True)

print("the day's slices: how much is behind us, how much is left")
day = build(series(DAY, {6: 100, 7: 500, 8: 900, 12: 800, 17: 200, 18: 50}, 23),
            [Plane(4.0, -90), Plane(4.0, 90)], 0.85)
total = (100 + 500 + 900 + 800 + 200 + 50) * 8.0 * 0.85 / 1000.0
check("day_kwh: the whole day", abs(day.day_kwh(DAY) - total) < 0.01, "%.3f" % day.day_kwh(DAY))
sofar = (100 + 500 + 900) * 8.0 * 0.85 / 1000.0
check("until_kwh: up to and including the current hour",
      abs(day.until_kwh(DAY, DAY + "T08:00") - sofar) < 0.01, "%.3f" % day.until_kwh(DAY, DAY + "T08:00"))
rest = (800 + 200 + 50) * 8.0 * 0.85 / 1000.0
check("remaining_kwh: strictly after the current hour",
      abs(day.remaining_kwh(DAY, DAY + "T08:00") - rest) < 0.01, "%.3f" % day.remaining_kwh(DAY, DAY + "T08:00"))
check("a different day is not mixed in", day.day_kwh("2026-09-24") == 0.0)

print("the factor: measured against expected, with a guard")
check("factor(): plain ratio", factor(7.5, 10.0) == 0.75, str(factor(7.5, 10.0)))
check("factor(): a flat zero is 'no basis', not a factor of 0.0 (a dead sensor looks the same)",
      factor(0.0, 5.0) is None, str(factor(0.0, 5.0)))
check("factor(): a few watt-seconds at dawn are not an anomaly either",
      factor(0.004, 27.95) is None, str(factor(0.004, 27.95)))
check("factor(): too little expected power is no basis at all", factor(0.4, 0.29) is None)
check("factor(): nothing measured yet is no factor", factor(None, 5.0) is None)
check("factor(): an absurd value is refused, not applied", factor(30.0, 5.0) is None)
check("factor(): ...and the refusal is not silent", factor(0.1, 9.0) is None)

print("PvForecast: fetching, merging the planes, and never breaking the cycle")
with tempfile.TemporaryDirectory() as tmp:
    calls: list = []
    two_planes = {"azimuth=-90": series(DAY, {12: 400.0}), "azimuth=90": series(DAY, {12: 400.0})}
    clock = [1_000_000.0]
    fc = PvForecast(planes=[Plane(4.0, -90), Plane(4.0, 90)], lat=49.12, lon=8.40, pr=0.85,
                    every_s=3600, cache_path=os.path.join(tmp, "fc.json"),
                    fetcher=fake_fetcher(two_planes, calls), now_fn=lambda: clock[0],
                    local_now=lambda: DAY + "T13:00")
    fc.update()
    check("update(): one request per plane", len(calls) == 2, str(len(calls)))
    check("update(): the planes are summed, not averaged",
          abs(fc.data.dc_wh[DAY + "T12:00"] - 3200.0) < 0.01, str(fc.data.dc_wh[DAY + "T12:00"]))
    check("update(): not stale after a good fetch", fc.stale is False)
    check("update(): the cache is written", os.path.exists(os.path.join(tmp, "fc.json")))
    fc.update()
    check("update(): the cadence is respected (no second fetch)",
          len(calls) == 2 and fc.fetches == 1, "calls=%d fetches=%d" % (len(calls), fc.fetches))
    clock[0] += 3601.0
    fc.update()
    check("update(): ...and it fetches again once the hour is up", fc.fetches == 2, str(fc.fetches))

    # A dead network must not cost the day: keep the last answer, flag it, keep counting.
    def broken(_url: str) -> dict:
        raise OSError("no route to host")

    fc._fetch = broken
    clock[0] += 7200.0
    fc.update()
    check("a failed fetch keeps the previous forecast", fc.data is not None and fc.stale is True)
    check("a failed fetch is counted, not raised", fc.failures == 1 and "OSError" in fc.last_error,
          fc.last_error)
    check("a failed fetch does not stop the cycle (no exception escaped)", True)

    # Restart: a fresh instance reads the last good answer from disk and says it is old.
    fc2 = PvForecast(planes=[Plane(4.0, -90)], lat=49.12, lon=8.40, cache_path=os.path.join(tmp, "fc.json"),
                     local_now=lambda: DAY + "T13:00")
    check("a restart reads the cached forecast back", fc2.data is not None and fc2.stale is True)
    check("...and marks it as not freshly fetched", fc2.as_state().get("stale") is True)

print("what the UI gets: numbers, a factor, and a shadow verdict that decides nothing")
st = fc.as_state(measured_today_kwh=1.6, soc=50.0, soc_target=75.0, capacity_kwh=10.0,
                 house_rest_kwh=3.0, margin=1.3)
check("as_state(): measured and expected are both reported",
      st["measured_kwh"] == 1.6 and "expected_so_far_kwh" in st)
check("as_state(): the factor is measured/expected for the same window",
      st["factor_today"] == factor(1.6, st["expected_so_far_kwh"]), str(st.get("factor_today")))
check("as_state(): the battery's need is derived from the target SOC",
      abs(st["battery_need_kwh"] - 2.5) < 0.01, str(st.get("battery_need_kwh")))
check("as_state(): the verdict is one of exactly two regimes",
      st.get("would_be") in ("car", "battery"), str(st.get("would_be")))
# The word check has to be about the *keys*: "factor" itself contains "act", so scanning the
# whole JSON for substrings is a trap this test already fell into once.
control_words = {"charge", "current", "command", "act", "steer", "set", "target_current",
                 "mode", "enable", "switch"}
check("as_state(): publishes no control vocabulary at all",
      not (set(st.keys()) & control_words), str(sorted(set(st.keys()) & control_words)))
no_soc = fc.as_state(measured_today_kwh=1.6)
check("as_state(): without SOC there is no verdict at all", "would_be" not in no_soc)
off = PvForecast(planes=[], lat=0, lon=0, enabled=False,
                 cache_path=os.path.join(tmp, "off.json")).as_state()
check("as_state(): disabled means disabled", off["enabled"] is False and "today_kwh" not in off)

print("the day's bookkeeping (main.Service helpers, driven without a service)")
stub = types.SimpleNamespace(_fc_cfg={"csv": os.path.join(tmp, "pv.csv"),
                                      "today": os.path.join(tmp, "pv_today.json")},
                             _fc_day=DAY, _fc=None, forecast=fc)
stub._fc_blank = lambda day: Service._fc_blank(stub, day)      # unbound helpers, instance-shape
stub._fc_row = lambda final: Service._fc_row(stub, final)
Service._fc_reset(stub, DAY)
site = SiteState(pv_power_w=4500.0, inverter_ac_w=4000.0, grid_power_w=-1000.0,
                 battery_power_w=-2000.0, battery_soc=52.0)
Service._fc_integrate(stub, site, 3600.0, car_w=0.0)
check("integrate(): AC energy over one hour", abs(stub._fc["ac_kwh"] - 4.0) < 0.001,
      str(stub._fc["ac_kwh"]))
check("integrate(): the array side is kept separately (DC)", abs(stub._fc["array_kwh"] - 4.5) < 0.001,
      str(stub._fc["array_kwh"]))
check("integrate(): the battery's charge is not double counted as house load",
      abs(stub._fc["house_kwh"] - 3.0) < 0.001, str(stub._fc["house_kwh"]))
check("integrate(): a charging battery becomes charge energy",
      abs(stub._fc["charge_kwh"] - 2.0) < 0.001, str(stub._fc["charge_kwh"]))
check("integrate(): SOC range is tracked", stub._fc["soc_min"] == 52.0 == stub._fc["soc_max"])
Service._fc_integrate(stub, None, 60.0)
check("integrate(): a missing site reading adds nothing and raises nothing",
      stub._fc["ac_kwh"] == 4.0 and stub._fc["samples"] == 1)
Service._fc_integrate(stub, SiteState(pv_power_w=0.0, inverter_ac_w=0.0, grid_power_w=2000.0,
                                      battery_power_w=0.0, battery_soc=40.0), 3600.0)
check("integrate(): a discharging battery at night lands in the house load, not the array",
      abs(stub._fc["house_kwh"] - 5.0) < 0.001 and stub._fc["array_kwh"] == 4.5,
      "house=%.3f array=%.3f" % (stub._fc["house_kwh"], stub._fc["array_kwh"]))

row = Service._fc_row(stub, False)
check("the partial row names what it is", row["row"] == "partial" and row["date"] == DAY)
check("the row carries both factors, for both sides",
      "factor_ac" in row and "factor_array" in row, str(sorted(row.keys())))
check("the row carries house, car and battery", all(
    k in row for k in ("house_kwh", "car_kwh", "battery_charge_kwh", "battery_discharge_kwh")))
check("the row counts its samples (the evidence's resolution)",
      row["samples"] == 2, str(row.get("samples")))
Service._fc_flush(stub, final=False)
check("the partial row is written to its own file",
      os.path.exists(os.path.join(tmp, "pv_today.json")))
check("...and not yet appended to the CSV", not os.path.exists(os.path.join(tmp, "pv.csv")))
Service._fc_flush(stub, final=True)
lines = open(os.path.join(tmp, "pv.csv")).read().strip().split("\n")
check("the finished day is appended to the CSV with a header", len(lines) == 2, str(len(lines)))
check("the CSV header and the values line up",
      lines[0].split(",") == list(row.keys()) and len(lines[1].split(",")) == len(row),
      lines[0][:60])
check("the final row is marked final", lines[1].split(",")[0] == "final", lines[1][:40])
check("a restart resumes today's partial row",
      (lambda: (setattr(stub, "_fc_day", ""), setattr(stub, "_fc", Service._fc_blank(stub, "")),
                Service._fc_load_today(stub), stub._fc["ac_kwh"] == 4.0)[-1])())

print("the inverter's own production figure: exact, and never fabricated")
check("day_delta(): the advance since the day's baseline",
      abs(day_delta(29267.336, 29240.1) - 27.236) < 0.001, str(day_delta(29267.336, 29240.1)))
check("day_delta(): no baseline -> no figure", day_delta(29267.336, None) is None)
check("day_delta(): no reading -> no figure", day_delta(None, 29240.1) is None)
check("day_delta(): a counter that went backwards is not a small number",
      day_delta(29240.1, 29267.336) is None)
stub._fc_se_latch = lambda st: Service._fc_se_latch(stub, st)
stub._fc = Service._fc_blank(stub, DAY)
stub._fc_se_latch(SiteState(inverter_energy_kwh=29240.0))
check("the baseline is latched on the first reading, with no figure yet",
      stub._fc["se_start_kwh"] == 29240.0 and stub._fc["se_kwh"] is None
      and stub._fc["se_partial"] is False)
stub._fc_se_latch(SiteState(inverter_energy_kwh=29267.336))
check("the next reading gives the day's production", stub._fc["se_kwh"] == 27.336,
      str(stub._fc["se_kwh"]))
check("...and remembers where the counter ended", stub._fc["se_end_kwh"] == 29267.336)
stub._fc_se_latch(SiteState(inverter_energy_kwh=29200.0))
check("a counter that jumps back keeps the last good figure and re-latches",
      stub._fc["se_kwh"] == 27.336 and stub._fc["se_start_kwh"] == 29200.0)
stub._fc_se_latch(SiteState(inverter_energy_kwh=None))
check("a missing reading changes nothing", stub._fc["se_kwh"] == 27.336)
check("the day's row carries it", Service._fc_row(stub, False)["se_production_kwh"] == 27.336)
# A baseline taken in the middle of the day describes only part of it, and must say so.
stub._fc = Service._fc_blank(stub, DAY)
stub._fc["samples"] = 5
stub._fc_se_latch(SiteState(inverter_energy_kwh=29240.0))
check("a baseline latched mid-day is marked partial", stub._fc["se_partial"] is True)
check("...and the row says so too", Service._fc_row(stub, False)["se_partial"] == 1)
# ...but a baseline taken at the day's start is not, even with the clock as the only witness:
# this is what the cycle order has to guarantee, because the flag is read from the day's own
# energy - and a version that ran after the cycle's sample could never report "complete".
stub._fc = Service._fc_blank(stub, DAY)
stub._fc_se_latch(SiteState(inverter_energy_kwh=29240.0), minutes_since_midnight=3)
check("a baseline taken at the day's start is complete", stub._fc["se_partial"] is False,
      str(stub._fc["se_partial"]))
stub._fc = Service._fc_blank(stub, DAY)
stub._fc_se_latch(SiteState(inverter_energy_kwh=29240.0), minutes_since_midnight=420)
check("a baseline taken later in the day is partial (the clock alone is enough)",
      stub._fc["se_partial"] is True)
stub._fc = Service._fc_blank(stub, DAY)
stub._fc_se_latch(SiteState(inverter_energy_kwh=29240.0), minutes_since_midnight=None)
check("an unknown clock is treated as 'not at the start' (a false warning is cheap)",
      stub._fc["se_partial"] is True)

print("step 1's promise: this forecast cannot steer anything")
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "evcharge", "pv_forecast.py")).read()
body = src.split('"""', 2)[-1]        # code only - the docstring may explain what it is not
for banned in ("set_max_current", "set_charging", "write_single", "write_multiple",
               "set_battery", "set_neutral"):
    check("pv_forecast.py has no actuator: %s" % banned, banned not in body)
ctl = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "evcharge", "controller.py")).read()
check("the controller does not read the forecast at all - 'forecast' appears nowhere in it",
      "forecast" not in ctl.lower(), "the controller must stay unable to see it")
check("...and the module docstring still says so, for the next reader",
      "no control" in src.split('"""')[1].lower() or "nothing here steers" in src.lower())

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all PV forecast checks PASS")
