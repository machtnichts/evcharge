#!/usr/bin/env python3
"""Tests for the site read cadence and backoff (evcharge.main.site_interval_s).

This is the schedule that decides how much the inverter is asked to do: every cycle
while a vehicle is connected, rarely when the driveway is empty, and progressively
longer waits after failures. Pure function - no hardware, no proxy.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.main import (  # noqa: E402
    site_interval_s, SITE_ACTIVE_S, SITE_IDLE_S, SITE_FAIL_BASE_S, SITE_FAIL_MAX_S,
)

FAILS = []


def check(name, cond, detail=""):
    print("  %-64s %s%s" % (name, "PASS" if cond else "FAIL", (" - " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


print("with a vehicle connected the site is read every cycle")
check("connected, no failures -> 30 s", site_interval_s(True, 0) == SITE_ACTIVE_S,
      "%s" % site_interval_s(True, 0))

print("with no vehicle the inverter is left alone")
check("idle, no failures -> 300 s (5 min)", site_interval_s(False, 0) == SITE_IDLE_S,
      "%s" % site_interval_s(False, 0))
check("idle interval is never shortened by a backoff", site_interval_s(False, 1) == SITE_IDLE_S,
      "%s" % site_interval_s(False, 1))

print("after failures the wait doubles, then holds at the cap")
seq = [site_interval_s(True, n) for n in (0, 1, 2, 3, 4, 5, 6, 7)]
check("30 (base) -> 30 -> 60 -> 120 -> 240 -> 480 -> 600 (capped)",
      seq == [30, 30, 60, 120, 240, 480, 600, 600], str(seq))
check("cap holds however long it keeps failing", site_interval_s(True, 99) == SITE_FAIL_MAX_S,
      "%s" % site_interval_s(True, 99))
check("first backoff step is the documented base", site_interval_s(True, 1) == SITE_FAIL_BASE_S,
      "%s" % site_interval_s(True, 1))
check("never shorter than the base cadence", site_interval_s(False, 1) >= SITE_IDLE_S,
      "%s" % site_interval_s(False, 1))
check("monotonic while failing", all(site_interval_s(True, n) <= site_interval_s(True, n + 1)
                                     for n in range(8)),
      str([site_interval_s(True, n) for n in range(8)]))

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all cadence/backoff checks PASS")