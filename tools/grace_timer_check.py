#!/usr/bin/env python3
"""Exercise the disable-grace path without disturbing a running charge.

Raises residual_power_w so the surplus collapses, watches for the app to start its
disable delay, then restores the setting - all well inside the 180 s window. While
the grace timer runs the decision still holds charge=True and target 0 A, so no
current command is sent and the car keeps charging untouched.

Usage: python3 tools/grace_timer_check.py [collapse_watts]
"""
import json
import os
import sys
import time
import urllib.request

APP = "http://127.0.0.1:7080"
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def get(path):
    with urllib.request.urlopen(APP + path, timeout=8) as r:
        return json.load(r)


def post(path, body):
    req = urllib.request.Request(APP + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def sample():
    st = get("/api/state")
    d = st.get("decision") or {}
    return st, d


def main():
    collapse = float(sys.argv[1]) if len(sys.argv) > 1 else 3000.0
    with open(CONFIG) as fh:
        restore = float((json.load(fh).get("settings") or {}).get("residual_power_w", 230))

    st, d = sample()
    print("control  : %s" % ("ON" if st.get("control_enabled") else "OFF (dry run)"))
    print("before   : charge=%s  reason=%r" % (d.get("charge"), d.get("reason")))
    if "waiting disable delay" in (d.get("reason") or ""):
        print("(already counting - a real dip is in progress)")
    print("collapse : residual_power_w -> %.0f W (restore value is %.0f W)"
          % (collapse, restore))
    post("/api/settings", {"residual_power_w": collapse})

    seen = False
    deadline = time.time() + 75
    while time.time() < deadline:
        time.sleep(10)
        st, d = sample()
        r = d.get("reason") or ""
        ch = st.get("charger") or {}
        print("  %s  charge=%-5s target=%5.1fA box_amp=%-3s flow=%.1fA  %s"
              % (time.strftime("%H:%M:%S"), d.get("charge"), d.get("target_current", 0),
                 ch.get("max_current"), max(ch.get("currents") or [0]), r))
        if "waiting disable delay" in r:
            seen = True

    print("restore  : residual_power_w -> %.0f W" % restore)
    post("/api/settings", {"residual_power_w": restore})
    time.sleep(35)
    st, d = sample()
    print("after    : charge=%s  reason=%r" % (d.get("charge"), d.get("reason")))
    ch = st.get("charger") or {}
    print("car      : car_state=%s charging=%s flowing_max=%.1f A"
          % (ch.get("car_state"), ch.get("charging"), max(ch.get("currents") or [0])))
    print()
    print("disable grace started counting : %s" % ("YES" if seen else "NO"))
    print("charge survived the dip        : %s" % ("YES" if d.get("charge") else "NO"))
    return 0 if seen else 1


if __name__ == "__main__":
    raise SystemExit(main())