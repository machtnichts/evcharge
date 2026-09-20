#!/usr/bin/env python3
"""Watch a charge: the app's decisions and the wallbox's reaction, side by side.

Read-only. Usage: python3 tools/watch_charge.py [seconds] [interval]
"""
import json
import os
import sys
import time
import urllib.request

APP = "http://127.0.0.1:7080/api/state"
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def get(url):
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.load(r)


def w(x):
    return "-" if x is None else ("%.2f kW" % (x / 1000.0) if abs(x) >= 1000 else "%.0f W" % x)


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    with open(CONFIG) as fh:
        host = json.load(fh).get("charger", {}).get("host")

    print("time      ctrl | decision                         | act | wallbox: car/alw/amp/flowing/phases | grid     pv    soc")
    end = time.time() + seconds
    while time.time() < end:
        try:
            s = get(APP)
            d = s.get("decision") or {}
            site = s.get("site") or {}
            line = "%s %5s | %-30s | %-3d | " % (
                time.strftime("%H:%M:%S"), "ON" if s.get("control_enabled") else "off",
                (("CHARGE %.1fA " % d.get("target_current", 0)) if d.get("charge")
                 else "no charge ") + (d.get("reason", "") or "")[:20],
                len(s.get("actions") or []))
            try:
                b = get("http://%s/status" % host)
                nrg = b.get("nrg") or []
                per = [float(x) / 10.0 for x in nrg[3:6]]
                flowing = max(per or [0.0])
                # phases the vehicle uses: count the phases carrying current (pha is
                # the contactor wiring and reports three on this 1-phase vehicle)
                phases = sum(1 for x in per if x >= 0.5) if b.get("car") == "2" else 0
                line += "%s/%s/%-3s/%-5.1fA/%dp" % (
                    b.get("car"), b.get("alw"), b.get("amp"), flowing, phases)
            except Exception as exc:  # noqa: BLE001
                line += "wallbox: %s" % str(exc)[:20]
            line += " | %8s %8s %5s%%" % (w(site.get("grid_power_w")), w(site.get("pv_power_w")),
                                          round(site.get("battery_soc") or 0))
            print(line)
            if s.get("errors"):
                print("           errors: %s" % s["errors"][-1])
        except Exception as exc:  # noqa: BLE001
            print("%s  app unreachable: %s" % (time.strftime("%H:%M:%S"), exc))
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())