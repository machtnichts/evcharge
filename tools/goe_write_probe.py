#!/usr/bin/env python3
"""Probe the go-e write path through the app's real driver.

Sends a no-op write (amx = the current limit) and reports what happened, so the
control path can be verified without changing how the wallbox behaves.

Usage: python3 tools/goe_write_probe.py [amps]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.drivers.goe import GoEClient

CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def main():
    import json
    with open(CONFIG) as fh:
        cfg = json.load(fh)
    host = (cfg.get("charger") or {}).get("host")
    if not host:
        print("no charger host in config.json")
        return 1

    client = GoEClient(host, phase=int((cfg.get("charger") or {}).get("phase", 1)))
    st = client.status()
    print("read   : car=%s alw=%s amp=%d phases=%s fw=%s"
          % (st.car_state, st.enabled, st.max_current, st.phases or "-", st.firmware))

    amps = float(sys.argv[1]) if len(sys.argv) > 1 else float(st.max_current or 8)
    if amps == st.max_current:
        print("write  : amx=%d (same value as now - no behaviour change)" % amps)
    else:
        print("write  : amx=%d (CHANGES the charging current)" % amps)
    try:
        ok = client.set_max_current(amps)
    except IOError as exc:
        print("FAILED : %s" % exc)
        return 1
    print("result : accepted=%s via %s" % (ok, client._set_path))
    st2 = client.status()
    print("after  : max_current=%d car=%s (the box does not echo amx; amp stays %s)"
          % (st2.max_current, st2.car_state, st2.raw.get("amp")))
    return 0


if __name__ == "__main__":
    sys.exit(main())