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
    """A fake SDM: its two counters plus the inverter's, and none grows on its own."""

    def __init__(self, imp=9297.60, exp=2529.21, age=12.0, ok=True, pv=None, pv_age=25.0):
        self.imp, self.exp, self.age, self.ok = imp, exp, age, ok
        # The garage PV's counter also lives in Home Assistant and is read through the same
        # client, but it carries its own age: the inverter's poller sleeps at night.
        self.pv, self.pv_age = pv, pv_age
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.ok:
            return None
        return {"import_kwh": self.imp, "export_kwh": self.exp, "age_s": self.age,
                "pv_energy_kwh": self.pv, "pv_age_s": self.pv_age}

    def feed(self, imp_delta=0.0, exp_delta=0.0, pv_delta=0.0):
        self.imp += imp_delta
        self.exp += exp_delta
        if self.pv is not None:
            self.pv += pv_delta


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

print("the third figure: the meter's net corrected by the garage PV's own counter")
# Afternoon case: the car draws 7.0 kWh while the PV feeds 2.5 kWh into the same feeder, so
# the meter only sees 4.5 kWh of import. Corrected = 4.5 - 0 + 2.5 = 7.0 kWh.
m13, meter13, clock13, path13 = fresh(meter=Meter(pv=279.56))
m13.update(connected=False)
m13.update(connected=True)
check("the inverter's counter is latched as the session's PV baseline",
      m13.session["base_pv"] == 279.56, m13.session["base_pv"])
meter13.feed(imp_delta=4.5, pv_delta=2.5)
clock13.tick(3600)
m13.update(connected=True)
fig = m13.as_state()["session"]
check("the meter's own figure is the plain import", fig["import_kwh"] == 4.5, fig)
check("the garage PV's share comes from its own counter", fig["pv_kwh"] == 2.5, fig)
check("the corrected figure closes the balance", fig["corrected_kwh"] == 7.0, fig)
check("and its source is named", fig["pv_source"] == "counter", fig)
m13.update(connected=False)
m13.update(connected=False)
check("the finished row carries all three values",
      m13.last["corrected_kwh"] == 7.0 and m13.last["pv_kwh"] == 2.5
      and m13.last["goe_session_kwh"] == 0.0, m13.last)
with open(path13, newline="") as fh:
    rows13 = list(csv.reader(fh))
check("the CSV header grew the three columns",
      rows13[0][-3:] == ["garage_pv_kwh", "sdm_corrected_kwh", "pv_source"], rows13[0])
check("...and the row is written in that order",
      rows13[1][9] == "2.5" and rows13[1][10] == "7.0" and rows13[1][11] == "counter",
      rows13[1])

print("the inverter's 0.00 slip is ignored, not turned into energy")
# Measured on the plant: the register reads 0.00 kWh for minutes after the logger wakes up
# (poller journal 05:16:53), then the real 279.56. Taking that literally would invent a huge
# negative correction; dropping it silently would freeze the figure without saying so.
m14, meter14, clock14, path14 = fresh(meter=Meter(pv=279.56))
m14.update(connected=False)
m14.update(connected=True)
meter14.feed(imp_delta=3.0, pv_delta=1.0)
clock14.tick(1800)
m14.update(connected=True)
meter14.pv = 0.0                       # the logger woke up and lied
m14.update(connected=True)
fig = m14.as_state()["session"]
check("a counter reading below the running maximum is dropped", fig["pv_kwh"] == 1.0, fig)
check("...and counted, so a frozen correction stays visible",
      m14.as_state()["pv_artefacts"] == 1, m14.as_state()["pv_artefacts"])
meter14.pv = 280.6                     # the real register comes back
m14.update(connected=True)
check("a later, higher reading is accepted again",
      m14.as_state()["session"]["pv_kwh"] == 1.04, m14.as_state()["session"])
m14.update(connected=False)
m14.update(connected=False)
check("and the row says the slip happened", "reported 0.00" in (m14.last["note"] or ""), m14.last)

print("a session without a counter reading says so instead of guessing")
m15, meter15, clock15, path15 = fresh(meter=Meter(pv=None))     # logger asleep (night)
m15.update(connected=False)
m15.update(connected=True)
meter15.feed(imp_delta=9.8)
clock15.tick(7200)
m15.update(connected=True)
fig = m15.as_state()["session"]
check("the correction falls back to zero",
      fig["corrected_kwh"] == 9.8 and fig["pv_kwh"] is None, fig)
check("the source says it was assumed", fig["pv_source"] == "assumed_zero", fig)
check("...and that it is still waiting rather than measured", fig["pv_waiting"] is True, fig)
check("a night session is then exactly the meter's figure",
      fig["net_kwh"] == fig["corrected_kwh"], fig)
m15.update(connected=False)
m15.update(connected=False)
check("and the row explains the assumption",
      "correction taken as 0" in (m15.last["note"] or ""), m15.last)

print("a stale counter is not treated as a still inverter")
m16, meter16, clock16, path16 = fresh(meter=Meter(pv=279.56, pv_age=4000.0))
m16.update(connected=False)
m16.update(connected=True)
meter16.feed(imp_delta=2.0, pv_delta=0.5)
clock16.tick(600)
m16.update(connected=True)
fig = m16.as_state()["session"]
check("an over-age counter reading is refused",
      fig["pv_source"] == "assumed_zero" and fig["pv_waiting"] is True, fig)
check("...and its age is published so the difference stays visible",
      m16.as_state()["pv_age_s"] == 4000.0, m16.as_state()["pv_age_s"])
check("...so the correction stays at zero and the state shows the staleness",
      fig["corrected_kwh"] == 2.0 and m16.as_state()["pv_stale"] is True, fig)

print("a 0.00 as the very first reading must not become the baseline")
# The trap this guards: the register's post-wake 0.00 latched as the *baseline* would turn the
# next real reading (279.56 kWh) into a ~279 kWh correction for a single session.
m17, meter17, clock17, path17 = fresh(meter=Meter(pv=0.0))
m17.update(connected=False)
m17.update(connected=True)
check("a zero reading is refused outright",
      m17.session["base_pv"] is None, m17.session["base_pv"])
check("...and counted as an artefact", m17.as_state()["pv_artefacts"] == 2,   # two cycles
      m17.as_state()["pv_artefacts"])
meter17.pv = 279.56                                     # the real register appears
meter17.feed(imp_delta=5.0)
clock17.tick(900)
m17.update(connected=True)
fig = m17.as_state()["session"]
check("the real reading becomes the baseline, not a 279 kWh correction",
      fig["pv_kwh"] == 0.0 and fig["corrected_kwh"] == 5.0, fig)
check("and a baseline latched after plug-in is flagged as partial",
      fig["pv_late"] is True, fig)
m17.update(connected=False)
m17.update(connected=False)
check("...and the row says the correction covers only part of the session",
      "latched late" in (m17.last["note"] or ""), m17.last)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all SDM session-meter checks PASS")
