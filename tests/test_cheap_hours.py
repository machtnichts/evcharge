#!/usr/bin/env python3
"""Tests for the cheap-hours window and the house's own clock.

The window is wall-clock, so the zone decides when the car actually charges: with the
window read against a UTC host clock, "00:00-05:00" fires an hour or two late for a
house in central Europe. These tests pin the conversion, the DST behaviour (the whole
reason for using a zone name rather than a fixed offset) and the countdown the UI shows.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.controller import (  # noqa: E402
    _in_window, _parse_hm, plant_tz, plant_now, cheap_window_info,
    ChargingController, Settings, MODE_CHEAP,
)

FAILS = []


def check(name, cond, detail=""):
    print("  %-68s %s%s" % (name, "PASS" if cond else "FAIL",
                            (" - " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


print("the window itself: plain, crossing midnight, and rubbish")
check("00:00-05:00 contains 03:00", _in_window(_parse_hm("03:00"), "00:00-05:00"))
check("00:00-05:00 excludes 05:00 (end is exclusive)", not _in_window(_parse_hm("05:00"), "00:00-05:00"))
check("23:00-04:00 contains 01:00 (crosses midnight)", _in_window(_parse_hm("01:00"), "23:00-04:00"))
check("23:00-04:00 excludes 12:00", not _in_window(_parse_hm("12:00"), "23:00-04:00"))
check("a window with no dash is ignored", not _in_window(_parse_hm("01:00"), "nonsense"))

print("the zone is resolved by name, and a bad name degrades instead of failing")
check("empty name -> host clock", plant_tz("") is None)
check("Europe/Berlin resolves", plant_tz("Europe/Berlin") is not None)
check("whitespace is tolerated", plant_tz("  Europe/Berlin ") is not None)
check("an unknown name -> None (host clock, no crash)", plant_tz("Bogus/Nowhere") is None)

print("wall-clock conversion, and correct DST on both sides of the year")
winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
check("January 12:00 UTC -> 13:00 in Berlin (CET)", plant_now("Europe/Berlin", winter).hour == 13,
      plant_now("Europe/Berlin", winter).strftime("%H:%M"))
check("July 12:00 UTC -> 14:00 in Berlin (CEST)", plant_now("Europe/Berlin", summer).hour == 14,
      plant_now("Europe/Berlin", summer).strftime("%H:%M"))
check("naive input is taken as the wall clock already", plant_now("Europe/Berlin", datetime(2026, 1, 15, 9, 5)) == datetime(2026, 1, 15, 9, 5))
check("host-clock mode converts an aware input to the host's wall clock",
      plant_now("", winter) == winter.astimezone().replace(tzinfo=None))

print("what the UI shows: the hours, in whose clock, and how long until it opens")
s = Settings(mode=MODE_CHEAP, timezone="Europe/Berlin", cheap_hours=["00:00-05:00"])
info = cheap_window_info(s, summer)          # 12:00 UTC = 14:00 local in July
check("the zone is named", info["tz"] == "Europe/Berlin", info["tz"])
check("the local time is the house's, not the host's", info["local"] == "14:00", info["local"])
check("outside the window", info["inside"] is False)
check("countdown to the next opening (10h from 14:00)", info["starts_in_min"] == 600,
      info["starts_in_min"])
info2 = cheap_window_info(Settings(cheap_hours=["23:00-04:00"], timezone="Europe/Berlin"),
                          datetime(2026, 1, 15, 17, 35, tzinfo=timezone.utc))   # 18:35 local
check("crossing-midnight window: 18:35 -> opens in 4h25m", info2["starts_in_min"] == 265,
      info2["starts_in_min"])
info3 = cheap_window_info(s, datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc))   # 00:30 local
check("just after midnight: inside the window", info3["inside"] is True, info3["local"])
check("inside the window there is no opening countdown", info3["starts_in_min"] is None)
check("host-clock mode labels itself as such",
      cheap_window_info(Settings(cheap_hours=["00:00-05:00"]), winter)["tz"] == "(host clock)")
check("no windows configured -> nothing shown, no crash",
      cheap_window_info(Settings(cheap_hours=[]), winter)["starts_in_min"] is None)

print("the zone drives the decision, not the host clock")
ctl = ChargingController(Settings(mode=MODE_CHEAP, timezone="Europe/Berlin",
                                  cheap_hours=["00:00-05:00"], min_current=6.0,
                                  max_current=14.0))
# 23:30 UTC in January is 00:30 in Berlin: inside the window although UTC is not
late_jan = plant_now("Europe/Berlin", datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc))
check("23:30 UTC in January is seen as 00:30 local", late_jan.strftime("%H:%M") == "00:30",
      late_jan.strftime("%H:%M"))
check("so the window is open (it would not be in UTC)", ctl._cheap_now(late_jan) is True)
evening = plant_now("Europe/Berlin", datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc))
check("21:00 UTC in January is 22:00 local -> window closed",
      ctl._cheap_now(evening) is False, evening.strftime("%H:%M"))

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all cheap-hours / time-zone checks PASS")