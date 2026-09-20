#!/usr/bin/env python3
"""Read the site once through the Modbus proxy and print raw registers plus the
decoded fields, so the sign conventions can be checked against reality.

Read-only: no writes, no cache priming beyond what the app already does.

    python3 tools/read_site_raw.py [host] [port]

Context (docs/REGISTERS.md):
    meter  M_AC_Power (40188+16)      positive = EXPORT   -> app negates
    inv    W (40069+12)               inverter AC output, already AC side
    inv    0xE174 float32 (vendor)    battery, positive = CHARGING
    app    SiteState.battery_power_w  positive = DISCHARGING  (see driver docstring)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.drivers.solaredge import SolarEdgeSite, s16, f32_swapped  # noqa: E402


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 1503

    site = SolarEdgeSite(host=host, port=port)
    try:
        st = site.read()
    finally:
        site.close()

    inv = st.raw["inverter"]
    bat = st.raw["bat"]
    meter = st.raw["meter"]

    inv_w = s16(inv[12]) * 10.0 ** s16(inv[13])
    dc_w = s16(inv[29]) * 10.0 ** s16(inv[30])
    bat_reg = f32_swapped(*bat)            # register convention: + = charging
    meter_w = s16(meter[16]) * 10.0 ** s16(meter[20])

    print("raw registers")
    print("  meter  M_AC_Power   %10.1f W   (+ = export)" % meter_w)
    print("  inv    W (AC out)   %10.1f W" % inv_w)
    print("  inv    DC power     %10.1f W" % dc_w)
    print("  vendor 0xE174       %10.1f W   (+ = charging)" % bat_reg)
    print("  vendor 0xE184 SOC   %10.1f %%" % f32_swapped(*st.raw["soc"]))
    print()
    print("decoded (what the app reports)")
    print("  pv_power_w          %10.1f W" % st.pv_power_w)
    print("  inverter_ac_w       %10.1f W" % st.inverter_ac_w)
    print("  grid_power_w        %10.1f W   (+ = import)" % st.grid_power_w)
    print("  battery_power_w     %10.1f W   app convention: + = discharging" % st.battery_power_w)
    print("  battery_soc         %10.1f %%" % (st.battery_soc or 0.0))
    print()
    print("consistency")
    print("  battery_charging_w      %10.1f W" % st.battery_charging_w)
    print("  battery_discharging_w   %10.1f W" % st.battery_discharging_w)
    print("  exporting_w             %10.1f W" % st.exporting_w)
    print("  importing_w             %10.1f W" % st.importing_w)
    print()
    # Physics: the inverter's AC output = DC bus power; the array = DC bus - battery
    # discharge. With the app's convention (+ = discharging) that is dc_w - battery_power_w
    # (using the REGISTER value, which this probe prints above as 0xE174).
    print("  array DC = dc - battery(register) = %.1f W   (pv_power_w = %.1f W)"
          % (dc_w - bat_reg, st.pv_power_w))
    print("  note: the AC output is the inverter's own W register = %.1f W" % inv_w)
    return 0


if __name__ == "__main__":
    sys.exit(main())