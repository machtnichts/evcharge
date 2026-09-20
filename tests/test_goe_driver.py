#!/usr/bin/env python3
"""go-e driver tests: v1 write path, amx (never amp), alw start/stop, real phases.

No hardware: FakeGoE models what this box (fw 041, API v1) actually does - /status
answers a fixture, /mqtt applies `amx` and `alw` writes, and `frc` writes are
answered with HTTP 200 while being ignored.
"""
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.drivers.goe import GoEClient

FAILS = []
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append(name)
    print("  %-62s %s%s" % (name, "PASS" if cond else "FAIL", (" - " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def nrg(amps=(0.1, 0.0, 0.0), volts=(226, 227, 228)):
    """Build an nrg array: V x3, I x3 (0.1 A), P x3 (0.1 kW), -, total (10 W), PF (0.01)."""
    currents = [int(round(a * 10)) for a in amps]
    powers = [int(round(v * a / 100.0)) for v, a in zip(volts, amps)]
    total = sum(v * a for v, a in zip(volts, amps))
    return list(volts) + currents + powers + [0, 0, int(round(total / 10.0)), 100]


def status_json(car="3", amp="8", pha="56", alw="0", nrg_data=None, fwv="041.0", eto="200470"):
    """A realistic fixture: shape copied from the live box."""
    return json.dumps({
        "car": car, "amp": amp, "pha": pha, "alw": alw, "fwv": fwv, "eto": eto,
        "err": "0", "cbl": "20", "ast": "0", "stp": "0", "dwo": "0", "tmp": "35",
        "nrg": nrg_data if nrg_data is not None else nrg(),
        "tma": [24.6, 23.6, 23.1, 23.4, -0.1, -0.1],
    })


class FakeGoE(GoEClient):
    """Fixture status + a small model of the box's setter behaviour."""

    def __init__(self, status=None, behaviour=None, simulate=False):
        super().__init__("127.0.0.1")
        self._status = status or status_json()
        self.behaviour = behaviour or {}
        self.simulate = simulate
        self.calls = []

    def _get(self, path):
        self.calls.append(path)
        if path.startswith("/status"):
            return self._status
        if self.simulate and path.startswith("/mqtt?payload="):
            # observed on the real box: amx changes the effective limit (reported back
            # in amp), alw takes effect, frc is ignored - and all answer with the
            # charger's full status dump over HTTP 200
            obj = json.loads(self._status)
            if "amx=" in path:
                obj["amp"] = path.split("amx=")[1].split("%26")[0][:2]
            if "alw=" in path:
                obj["alw"] = path.split("alw=")[1][:1]
            self._status = json.dumps(obj)
            return self._status
        for p, response in self.behaviour.items():
            if path.startswith(p):
                if isinstance(response, int):
                    raise urllib.error.HTTPError(p, response, "Not found", {}, None)
                return response
        raise urllib.error.HTTPError(path.split("?")[0], 404, "Not found", {}, None)


def make(**kw):
    return FakeGoE(**kw)


print("status decoding")
st = make().status()
check("connected but not charging", st.connected and not st.charging, st.car_state)
check("energy + firmware parsed", st.energy_total_kwh == 20047.0 and st.firmware == "041.0",
      "%.1f kWh fw=%s" % (st.energy_total_kwh, st.firmware))
check("nothing flowing -> 0 phases", st.phases == 0, str(st.phases))

print("phases come from the currents, never from pha (which is the contactor wiring)")
# measured on this box while charging on one phase: pha=63 (all six bits!) and
# currents [0.1, 7.9, 0.0]
st = make(status=status_json(car="2", pha="63", nrg_data=nrg((0.1, 7.9, 0.0)))).status()
check("1-phase vehicle -> 1p although pha says three", st.phases == 1,
      "pha ignored, got %dp" % st.phases)
st = make(status=status_json(car="2", pha="7", nrg_data=nrg((7.9, 7.8, 8.0)))).status()
check("3-phase vehicle -> 3p", st.phases == 3, "got %dp" % st.phases)
st = make(status=status_json(car="2", pha="63", nrg_data=nrg((0.1, 0.1, 0.1)))).status()
check("charging but nothing drawn -> 0p (no inventing phases)", st.phases == 0, "got %dp" % st.phases)
st = make(status=status_json(car="2", pha="63", nrg_data=nrg((0.1, 7.9, 0.0)))).status()
check("total power read from the box (1830 W)", abs(st.power - 1830) <= 20, "%.0f W" % st.power)

print("current limit goes out as amx (flash-free), never amp")
c = make(simulate=True)
c.set_max_current(11.4)
writes = [p for p in c.calls if "amx=" in p or "amp=" in p]
check("uses the v1 setter path", any(p.startswith("/mqtt?payload=") for p in writes), str(writes))
check("sends amx", any("amx=11" in p for p in writes), str(writes))
check("never sends amp", not any("amp=" in p.replace("amx=", "") for p in writes), str(writes))
check("accepts the status-dump response as success", c._set_path == "/mqtt?payload=",
      str(c._set_path))
check("the box reports the amx as its effective limit", c.status().max_current == 11,
      str(c.status().max_current))
c.set_max_current(2)
check("clamps low to 6 A", "amx=6" in c.calls[-1], c.calls[-1])
c.set_max_current(40)
check("clamps high to 32 A", "amx=32" in c.calls[-1], c.calls[-1])

print("start/stop uses alw (readable) and verifies the write")
c = make(simulate=True)
check("start writes alw=1", c.set_charging(True) and any("alw=1" in p for p in c.calls), str(c.calls))
check("start is confirmed by reading alw back", c.status().enabled, str(c.status().raw.get("alw")))
c = make(status=status_json(alw="1"), simulate=True)
check("stop writes alw=0", c.set_charging(False) and not c.status().enabled, str(c.calls))

print("the trap: frc is accepted with 200 but ignored, so it must not pass as success")
c = make(status=status_json(alw="0"), behaviour={"/mqtt?payload=": status_json(alw="0")})
try:
    c.set_charging(True)
    check("frc-only box cannot fake a start", False, "no exception")
except IOError as exc:
    msg = str(exc)
    check("frc-only box cannot fake a start", "alw" in msg and "frc" in msg, msg)
check("alw was tried first, so the app does not rely on a 200", any("alw=1" in p for p in c.calls),
      str(c.calls))

print("fallbacks and error reporting")
c = make(behaviour={"/mqtt?payload=": 404, "/api/set": '{"amx":true}'})
ok = c.set_max_current(9)
check("falls back to the v2 form when /mqtt 404s", ok and c._set_path == "/api/set?", str(c.calls))
calls_before = len(c.calls)
c.set_max_current(10)
check("remembers the working form", len(c.calls) == calls_before + 1
      and c.calls[-1].startswith("/api/set?"), str(c.calls[-1:]))

c = make(behaviour={"/mqtt?payload=": '{"success":false,"error":"bad key value"}'})
try:
    c.set_max_current(10)
    check("failure envelope raises with the box's message", False, "no exception")
except IOError as exc:
    check("failure envelope raises with the box's message", "bad key value" in str(exc), str(exc))

c = make(behaviour={})
try:
    c.set_max_current(10)
    check("all forms 404 -> actionable error", False, "no exception")
except IOError as exc:
    msg = str(exc)
    check("all forms 404 -> actionable error", "HTTP API" in msg and "/mqtt?payload=" in msg, msg)

print("\n%d passed, %d failed" % (len(CHECKS) - len(FAILS), len(FAILS)))
if FAILS:
    print("failed:", FAILS)
sys.exit(1 if FAILS else 0)