#!/usr/bin/env python3
"""Tests for the SDM session meter (evcharge.session_meter).

Pure logic: the clock and the meter readings are injected, so plug-in latches, deltas,
unplug finalisation, the CSV and the restart behaviour are all exercised without hardware.

The meter exists because the wallbox's own session figure under-reads. Its figure is the
*meters* view of the session - the garage PV feeds the same feeder, and the owner accepted
that distortion - so the tests pin the two things that must stay honest: the arithmetic
(import - export, never negative because of a counter reset) and the fact that a missing
reading or a flickering connector never invents or loses energy.
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.session_meter import SessionMeter, CSV_HEADER  # noqa: E402

FAILS = []
T0 = 1_800_000_000.0


def check(name, cond, detail=""):
    print("  %-70s %s%s" % (name, "PASS" if cond else "FAIL",
                            (" - " + str(detail)) if detail != "" else ""))
    if not cond:
        FAILS.append(name)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


class Meter:
    """A fake SDM: two counters that only grow unless a test says otherwise."""

    def __init__(self, imp=9297.60, exp=2529.21, age=12.0, ok=True):
        self.imp, self.exp, self.age, self.ok = imp, exp, age, ok
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.ok:
            return None
        return {"import_kwh": self.imp, "export_kwh": self.exp, "age_s": self.age}

    def feed(self, imp_delta=0.0, exp_delta=0.0):
        self.imp += imp_delta
        self.exp += exp_delta


def fresh(meter=None, clock=None, csv_path=None, **kw):
    meter = meter or Meter()
    clock = clock or Clock()
    path = csv_path or os.path.join(tempfile.mkdtemp(prefix="sdm-"), "sessions.csv")
    return SessionMeter(read=meter, csv_path=path, now_fn=clock, **kw), meter, clock, path


print("plug-in latches the meter, the difference is the session")
m, meter, clock, path = fresh()
m.update(connected=False)
check("no session while the car is away", m.session is None and m.last is None)
m.update(connected=True)
check("plugging in starts a session", m.session is not None and m.last is None)
check("the baseline is latched from the first reading",
      m.session["base_import"] == 9297.60 and m.session["base_export"] == 2529.21,
      m.session["base_import"])
check("the session starts at zero", m.as_state()["session"]["import_kwh"] == 0.0)
clock.tick(1800)
meter.feed(imp_delta=3.5)                       # the car took 3.5 kWh in 30 min
m.update(connected=True)
st = m.as_state()["session"]
check("the session follows the meter", st["import_kwh"] == 3.5, st)
check("...and reports its duration", st["duration_h"] == 0.5, st["duration_h"])
clock.tick(1800)
meter.feed(imp_delta=2.1)
m.update(connected=True)
check("it keeps counting", m.as_state()["session"]["import_kwh"] == 5.6,
      m.as_state()["session"]["import_kwh"])

print("the garage PV shows up in the export counter instead of being hidden")
m2, meter2, clock2, path2 = fresh()
meter2.exp = 2529.21
m2.update(connected=True)
clock2.tick(600)
meter2.feed(imp_delta=1.0)
m2.update(connected=True)
st = m2.as_state()["session"]
check("net = import - export", st["net_kwh"] == 1.0, st)
clock2.tick(600)
meter2.feed(exp_delta=1.6)                      # the sun beat the car that moment
m2.update(connected=True)
st = m2.as_state()["session"]
check("a PV-heavy moment gives a negative net, and says so with numbers",
      st["net_kwh"] == -0.6 and st["export_kwh"] == 1.6, st)

print("unplug freezes the session, writes the CSV, and shows it as the last one")
m3, meter3, clock3, path3 = fresh()
m3.update(connected=True)
clock3.tick(3600)
meter3.feed(imp_delta=7.25, exp_delta=0.5)
m3.update(connected=True, goe_session_kwh=6.8)
m3.update(connected=False)
check("one cycle without the car does not end the session (the read can flicker)",
      m3.session is not None and m3.last is None)
m3.update(connected=False)
check("two do", m3.session is None and m3.last is not None)
check("the frozen figures are the meter's difference",
      m3.last["import_kwh"] == 7.25 and m3.last["export_kwh"] == 0.5
      and m3.last["net_kwh"] == 6.75, m3.last)
check("...with the session's duration and the go-e figure for comparison",
      m3.last["duration_h"] == 1.0 and m3.last["goe_session_kwh"] == 6.8, m3.last)
check("the CSV has a header and one row", os.path.exists(path3))
with open(path3) as fh:
    rows = list(csv.reader(fh))
check("header first, then the session", rows[0] == CSV_HEADER and len(rows) == 2, rows)
check("the row carries the numbers, not a formatted string",
      rows[1][3] == "7.25" and rows[1][5] == "6.75" and rows[1][6] == "6.8", rows[1])
check("the session state is empty once it is finished",
      m3.as_state()["session"] is None and m3.as_state()["rows"] == 1)

print("a second session appends, and the last one survives a restart")
m4, meter4, clock4, path4 = fresh(csv_path=path3)
check("a fresh meter reads the last session from the CSV (restart)",
      m4.last is not None and m4.last["import_kwh"] == 7.25, m4.last)
m4.update(connected=True)
clock4.tick(1200)
meter4.feed(imp_delta=1.4)                      # a fresh baseline, not the old one
m4.update(connected=True)
check("the new session starts from the new plug-in, not from the old session",
      m4.as_state()["session"]["import_kwh"] == 1.4, m4.as_state()["session"])
m4.update(connected=False)
m4.update(connected=False)
with open(path3) as fh:
    rows = list(csv.reader(fh))
check("the CSV now has two rows and still one header",
      len(rows) == 3 and rows[0] == CSV_HEADER, len(rows))
check("the last session is the new one", m4.last["import_kwh"] == 1.4, m4.last)

print("a counter reset rebases instead of reporting nonsense")
m5, meter5, clock5, path5 = fresh()
m5.update(connected=True)
clock5.tick(600)
meter5.feed(imp_delta=2.0)
m5.update(connected=True)
meter5.imp = 100.0                              # the SDM was reset / replaced
clock5.tick(600)
meter5.feed(imp_delta=0.5)
m5.update(connected=True)
st = m5.as_state()["session"]
check("the figure never goes negative", st["import_kwh"] >= 0.0, st)
check("the rebase is counted and published", st["rebased"] == 1, st)
check("the energy counted before the reset is kept (it really did flow)",
      st["import_kwh"] == 2.0, st)
clock5.tick(600)
meter5.feed(imp_delta=0.5)
m5.update(connected=True)
st = m5.as_state()["session"]
check("...and counting continues from the new baseline", st["import_kwh"] == 2.5, st)

print("missing readings are a reading problem, never an energy invention")
m6, meter6, clock6, path6 = fresh()
meter6.ok = False
m6.update(connected=True)
check("a session starts even without a reading, and says it is waiting",
      m6.session is not None and m6.as_state()["session"]["waiting_for_meter"] is True,
      m6.as_state()["session"])
check("the failures are counted", m6.as_state()["read_failures"] >= 1)
meter6.ok = True
clock6.tick(300)
meter6.feed(imp_delta=1.1)
m6.update(connected=True)
st = m6.as_state()["session"]
check("the first good reading latches the baseline - it does not count from zero",
      st["waiting_for_meter"] is False and st["import_kwh"] == 0.0, st)
clock6.tick(300)
meter6.feed(imp_delta=1.0)
m6.update(connected=True)
check("from then on it counts", m6.as_state()["session"]["import_kwh"] == 1.0,
      m6.as_state()["session"])
m6.update(connected=False)
m6.update(connected=False)
check("a session whose first reading came late says so in the CSV note",
      "starts late" in (m6.last["note"] or ""), m6.last)
check("...and it still records what it could see", m6.last["import_kwh"] == 1.0, m6.last)

print("a frozen meter is refused, not turned into 0 kWh")
m10, meter10, clock10, path10 = fresh()
meter10.age = 41 * 3600.0                       # the SDM has not been read for 41 hours
m10.update(connected=True)
st10 = m10.as_state()
check("the stale reading is published as stale, with its age",
      st10["stale"] is True and st10["read_age_s"] > 3600, st10["stale"])
check("...and it is NOT used as a baseline", st10["session"]["waiting_for_meter"] is True,
      st10["session"])
meter10.age = 12.0                              # the polling works again
clock10.tick(600)
meter10.feed(imp_delta=4.0)
m10.update(connected=True)
st10 = m10.as_state()["session"]
check("a fresh reading latches the baseline and the staleness clears",
      st10["waiting_for_meter"] is False and st10["import_kwh"] == 0.0, st10)
clock10.tick(600)
meter10.feed(imp_delta=2.5)
m10.update(connected=True)
check("from then on it counts", m10.as_state()["session"]["import_kwh"] == 2.5,
      m10.as_state()["session"])
m10.update(connected=False)
m10.update(connected=False)
check("and the CSV row is a real measurement", m10.last["import_kwh"] == 2.5, m10.last)

m11, meter11, clock11, path11 = fresh()
meter11.age = 41 * 3600.0
m11.update(connected=True)
clock11.tick(3600)
m11.update(connected=False)
m11.update(connected=False)
check("a session measured on a frozen meter is flagged in the CSV, not written as 0 kWh",
      "stale" in (m11.last["note"] or "") and m11.last["import_kwh"] == 0.0, m11.last)

print("the staleness threshold is a setting, not a magic number")
m12, meter12, clock12, path12 = fresh(stale_s=300.0)
meter12.age = 400.0                             # older than 300 s -> stale
m12.update(connected=True)
check("older than the threshold counts as stale",
      m12.as_state()["session"]["waiting_for_meter"] is True)
meter12.age = 120.0                             # younger -> fine
m12.update(connected=True)
check("younger than the threshold is accepted",
      m12.as_state()["session"]["waiting_for_meter"] is False)

print("a session already running at process start is flagged")
m7, meter7, clock7, path7 = fresh()
m7.update(connected=True)                       # no prior unplug was ever seen
check("relatched is set when the car was already plugged in",
      m7.as_state()["session"]["relatched"] is True, m7.as_state()["session"])
m7.update(connected=False)
m7.update(connected=False)
check("...and the CSV row explains why the baseline is young",
      "baseline latched at process start" in (m7.last["note"] or ""), m7.last)
m8, meter8, clock8, path8 = fresh()
m8.update(connected=False)
m8.update(connected=True)
check("a normal plug-in is not flagged", m8.as_state()["session"]["relatched"] is False,
      m8.as_state()["session"])
check("...and it is not marked as waiting for the meter either",
      m8.as_state()["session"]["waiting_for_meter"] is False)

print("disabled means silent, and it always publishes a usable shape")
m9, meter9, clock9, path9 = fresh(enabled=False)
m9.update(connected=True)
check("nothing is read or latched when disabled",
      m9.session is None and meter9.calls == 0, meter9.calls)
st = m9.as_state()
check("the state shape stays complete", {"enabled", "session", "last", "csv", "rows"} <= set(st),
      sorted(st))
check("the csv path is published so the owner knows where the rows land",
      st["csv"].endswith("sessions.csv"), st["csv"])

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all SDM session-meter checks PASS")
