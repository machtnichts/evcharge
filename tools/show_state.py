#!/usr/bin/env python3
"""Print the charging app's live state as the web UI would label it.

Fetches /api/state from the running service and renders the same sign-dependent
labels the UI uses, so a sign-convention bug is visible from the command line.

    python3 tools/show_state.py [http://127.0.0.1:7080]
"""
import json
import sys
import urllib.request

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7080") + "/api/state"


def w(value):
    return "-" if value is None else "%.1f W" % value


def main() -> int:
    with urllib.request.urlopen(URL, timeout=5) as fh:
        d = json.load(fh)
    s = d["site"]

    bat = s.get("battery_power_w")
    soc = s.get("battery_soc")
    # mirrors the UI: >0 discharging, otherwise charging (driver docstring convention)
    bat_label = "charging" if (bat or 0) <= 0 else "discharging"

    print("mode              %s   (control_enabled=%s, dry_run=%s)"
          % (d.get("mode"), d.get("control_enabled"), d.get("dry_run")))
    print("cycles / age      %s / %ss" % (d.get("cycles"), d.get("age_s")))
    print("pv_power_w        %10s   (array DC production)" % w(s.get("pv_power_w")))
    print("inverter_ac_w     %10s   (inverter AC output)" % w(s.get("inverter_ac_w")))
    print("grid_power_w      %10s   (+ = import)" % w(s.get("grid_power_w")))
    print("battery_power_w   %10s   (+ = discharging, app convention)" % w(bat))
    print("battery_soc       %10.1f %%" % (soc if soc is not None else 0.0))
    print()
    print('battery state     %s %.0f W' % (bat_label, abs(bat or 0)))
    if d.get("decision"):
        dec = d["decision"]
        print("decision          charge=%s target=%sA  %s"
              % (dec.get("charge"), dec.get("target_current"), dec.get("reason")))
    return 0


if __name__ == "__main__":
    sys.exit(main())