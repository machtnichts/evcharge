#!/usr/bin/env python3
"""Tests for the charge-switching safety net (evcharge.safety).

The point of this thing is to notice a control loop that cannot hold a decision and stop
it before a car's on-board charger pays for it: count the *stops* the wallbox really saw,
latch a fault at the threshold, and let the app turn the charge on and go quiet.

Stops only, by the owner's decision (20.09.2026): a start is the counterpart of every
stop, so counting both directions reported one interrupted session as two events and
halved the tolerance. Current-limit writes (`amx=…`) are re-limiting, not switching, and
never count - that was the original misunderstanding this suite now pins down.

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


print("only stops count")
c = SwitchCounter(threshold=5)
check("the first write seeds the state, it is not a stop",
      c.note("alw=1", T) is None and c.count(T) == 0, c.count(T))
check("re-asserting the same state is not a stop either",
      c.note("alw=1", T + 30) is None and c.count(T + 30) == 0, c.count(T + 30))
check("stopping an enabled charge is", c.note("alw=0", T + 60) is None and c.count(T + 60) == 1,
      c.count(T + 60))
check("the start that follows does not count - it is the counterpart of the stop",
      c.note("alw=1", T + 90) is None and c.count(T + 90) == 1, c.count(T + 90))
check("so two stops in three starts is a count of two",
      c.note("alw=0", T + 120) is None and c.note("alw=1", T + 150) is None
      and c.count(T + 150) == 2, c.count(T + 150))
check("current-limit changes are not switching and never count",
      c.note("amx=6", T + 155) is None and c.note("amx=14", T + 160) is None
      and c.count(T + 160) == 2, c.count(T + 160))
c2 = SwitchCounter(threshold=2)
c2.note("alw=0", T)
check("an alw=0 that never followed an alw=1 is not a stop (nothing was running)",
      c2.count(T + 1) == 0, c2.count(T + 1))

print("the window slides - old stops are forgotten")
c = SwitchCounter(threshold=3)
c.note("alw=1", T)
c.note("alw=0", T + 60)
check("one stop inside the window", c.count(T + 120) == 1, c.count(T + 120))
check("still counted just before it ages out",
      c.count(T + DEFAULT_WINDOW_S - 1) == 1, c.count(T + DEFAULT_WINDOW_S - 1))
check("forgotten after the window passes", c.count(T + DEFAULT_WINDOW_S + 61) == 0,
      c.count(T + DEFAULT_WINDOW_S + 61))

print("at the threshold of stops it latches a fault")
c = SwitchCounter(threshold=3)
c.note("alw=1", T)
c.note("alw=0", T + 60)                       # 1 stop
check("below the threshold: no fault", not c.fault and c.count(T + 61) == 1)
c.note("alw=1", T + 120)                      # a restart between them counts nothing
c.note("alw=0", T + 180)                      # 2 stops
check("still below with two stops", not c.fault and c.count(T + 181) == 2, c.count(T + 181))
c.note("alw=1", T + 240)
reason = c.note("alw=0", T + 300)             # 3 stops -> latch
check("the third stop latches the fault", c.fault and reason, reason)
check("the reason says stops, how many, and over what window",
      "3 charging stops" in (reason or "") and "30" in (reason or ""), reason)
check("a latched fault stays latched even as the events age out",
      c.count(T + DEFAULT_WINDOW_S + 500) == 0 and c.fault, c.count(T + DEFAULT_WINDOW_S + 500))
check("and does not latch twice", c.note("alw=1", T + 320) is None)

print("clearing releases it")
c.clear()
check("no fault after clearing", not c.fault)
check("the counter starts over", c.count(T + 200) == 0, c.count(T + 200))
c.note("alw=1", T + 210)
c.note("alw=0", T + 220)                      # 1 stop
c.note("alw=1", T + 230)                      # counts nothing
check("a fresh count is possible afterwards, still below the limit",
      c.count(T + 231) == 1 and not c.fault, c.count(T + 231))
c.note("alw=0", T + 240)                      # 2 stops
check("two stops are still below the threshold of three",
      not c.fault and c.count(T + 241) == 2, c.count(T + 241))
c.note("alw=1", T + 250)
reason2 = c.note("alw=0", T + 260)            # 3 stops -> latch
check("and the third stop latches again", reason2 is not None and c.fault, reason2)

print("a restart clears the fault, by construction and by intent")
c = SwitchCounter(threshold=2)
c.note("alw=1", T)
c.note("alw=0", T + 60)                       # 1 stop
c.note("alw=1", T + 120)
check("a running counter latches", c.note("alw=0", T + 180) is not None and c.fault)
fresh = SwitchCounter(threshold=2)
check("a freshly built counter - what a service restart gives - is clean",
      not fresh.fault and fresh.count(T + 181) == 0)
check("and it is deliberately not persisted: no save/load to restore the latch from",
      not hasattr(fresh, "save") and not hasattr(fresh, "load"))
check("the latch still never decays while the app runs (that is the half that matters)",
      c.fault and c.count(T + DEFAULT_WINDOW_S + 500) == 0)

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
c.note("alw=0", T + 60)                       # 1 stop
check("one stop is below a threshold of two",
      c.as_state(T + 61)["changes"] == 1 and not c.fault, c.as_state(T + 61))
c.note("alw=1", T + 120)
c.note("alw=0", T + 180)                      # 2 stops -> latch
st2 = c.as_state(T + 3600)
check("a latched fault reports how long ago", st2["fault"] is True
      and abs(st2["fault_min_ago"] - 57.0) < 0.2, st2["fault_min_ago"])

print("the defaults are the ones the owner asked for, and the threshold has a floor")
check("default threshold is 5 stops", DEFAULT_THRESHOLD == 5, DEFAULT_THRESHOLD)
check("default window is 30 minutes", DEFAULT_WINDOW_S == 1800.0, DEFAULT_WINDOW_S)
check("a silly threshold cannot be configured away", SwitchCounter(threshold=0).threshold == 2)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all charge-switching safety checks PASS")
