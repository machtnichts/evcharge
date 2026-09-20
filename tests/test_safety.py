#!/usr/bin/env python3
"""Tests for the charge-switching safety net (evcharge.safety).

The point of this thing is to notice a control loop that cannot hold a decision and stop
it before a car's on-board charger pays for it: count the on/off edges the wallbox really
saw, latch a fault at the threshold, and let the app turn the charge on and go quiet.

Everything here is pure - injected timestamps, no sleeping, no hardware.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.safety import SwitchCounter, DEFAULT_THRESHOLD, DEFAULT_WINDOW_S  # noqa: E402

FAILS = []
T = 1_800_000_000.0


def check(name, cond, detail=""):
    print("  %-68s %s%s" % (name, "PASS" if cond else "FAIL",
                            (" - " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


print("only real changes count as transitions")
c = SwitchCounter(threshold=5)
check("a first write is not a transition (nothing to change from)",
      c.note("alw=1", T) is None and c.count(T) == 0, c.count(T))
check("re-asserting the same state is not a transition either",
      c.note("alw=1", T + 30) is None and c.count(T + 30) == 0, c.count(T + 30))
check("the opposite state is", c.note("alw=0", T + 60) is None and c.count(T + 60) == 1,
      c.count(T + 60))
check("and back again", c.note("alw=1", T + 90) is None and c.count(T + 90) == 2,
      c.count(T + 90))
check("non-alw commands are ignored", c.note("amx=6", T + 95) is None and c.count(T + 95) == 2)

print("the window slides - old edges are forgotten")
c = SwitchCounter(threshold=3)
c.note("alw=1", T)
c.note("alw=0", T + 60)
check("one edge inside the window", c.count(T + 120) == 1, c.count(T + 120))
check("still counted just before it ages out",
      c.count(T + DEFAULT_WINDOW_S - 1) == 1, c.count(T + DEFAULT_WINDOW_S - 1))
check("forgotten after the window passes", c.count(T + DEFAULT_WINDOW_S + 61) == 0,
      c.count(T + DEFAULT_WINDOW_S + 61))

print("at the threshold it latches a fault")
c = SwitchCounter(threshold=3)
c.note("alw=1", T)
c.note("alw=0", T + 60)                      # 1 edge
check("below the threshold: no fault", not c.fault and c.count(T + 61) == 1)
c.note("alw=1", T + 120)                     # 2 edges
check("still below", not c.fault, c.count(T + 121))
reason = c.note("alw=0", T + 180)            # 3 edges -> latch
check("the third edge latches the fault", c.fault and reason, reason)
check("the reason names the count and the window",
      "3 charging on/off changes" in (reason or "") and "30" in (reason or ""), reason)
check("a latched fault stays latched even as the events age out",
      c.count(T + DEFAULT_WINDOW_S + 500) == 0 and c.fault, c.count(T + DEFAULT_WINDOW_S + 500))
check("and does not latch twice", c.note("alw=1", T + 200) is None)

print("clearing releases it")
c.clear()
check("no fault after clearing", not c.fault)
check("the counter starts over", c.count(T + 200) == 0, c.count(T + 200))
c.note("alw=1", T + 210)                     # seeds the state, no edge yet
c.note("alw=0", T + 220)                     # 1
c.note("alw=1", T + 230)                     # 2
check("a fresh count is possible afterwards, still below the limit",
      c.count(T + 231) == 2 and not c.fault, c.count(T + 231))
reason2 = c.note("alw=0", T + 240)           # 3 -> latch
check("and the third edge latches again", reason2 is not None and c.fault, reason2)

print("the state published to the UI")
c = SwitchCounter(threshold=5)
c.note("alw=1", T)
c.note("alw=0", T + 60)
st = c.as_state(T + 120)
check("carries the count, window, limit and fault flag",
      st["changes"] == 1 and st["window_min"] == 30.0 and st["threshold"] == 5
      and st["fault"] is False, st)
check("no fault means no age", st["fault_min_ago"] is None, st["fault_min_ago"])
c = SwitchCounter(threshold=2)
c.note("alw=1", T)
c.note("alw=0", T + 60)                      # 1
c.note("alw=1", T + 120)                     # 2 -> latch
st2 = c.as_state(T + 3600)
check("a latched fault reports how long ago", st2["fault"] is True
      and abs(st2["fault_min_ago"] - 58.0) < 0.2, st2["fault_min_ago"])

print("the defaults are the ones the owner asked for, and the threshold has a floor")
check("default threshold is 5 transitions", DEFAULT_THRESHOLD == 5, DEFAULT_THRESHOLD)
check("default window is 30 minutes", DEFAULT_WINDOW_S == 1800.0, DEFAULT_WINDOW_S)
check("a silly threshold cannot be configured away", SwitchCounter(threshold=0).threshold == 2)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all charge-switching safety checks PASS")