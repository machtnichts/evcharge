#!/usr/bin/env python3
"""Decode-convention tests for the SolarEdge driver - no hardware, no proxy.

These pin the two sign conventions that are easy to get backwards, against canned
register content. They are also the oracle for a future port: the same vectors must
produce the same decoded fields.

Register conventions (device truth):
    meter  M_AC_Power (40188+16)     positive = EXPORT      -> app reports + import
    vendor 0xE174 float32            positive = CHARGING    -> app reports + discharging
    inv    DC power (40069+29)       the inverter's DC BUS (array + battery)
    app    SiteState.battery_power_w positive = DISCHARGING (driver docstring, UI, controller)
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.drivers.solaredge import (  # noqa: E402
    SolarEdgeSite, METER_DATA, METER_LEN, INV_DATA, INV_LEN, V_BAT_POWER, V_BAT_SOC,
    INV_START, INV_COUNT, METER_START, METER_COUNT, VENDOR_START, VENDOR_COUNT,
    BAT_OFF, SOC_OFF, INV_WH_OFF, INV_WH_SF_OFF,
)

FAILS = []


def check(name, cond, detail=""):
    print("  %-62s %s%s" % (name, "PASS" if cond else "FAIL", (" - " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def reg_words(value):
    """The two registers a device sends for a 'swapped' float32."""
    raw = struct.pack(">f", value)
    high = int.from_bytes(raw[0:2], "big")
    low = int.from_bytes(raw[2:4], "big")
    return [low, high]


def u16(v):
    """Signed register value as the unsigned 16-bit word the device sends."""
    return v & 0xFFFF


class FakeClient:
    """Serves canned register blocks; nothing is read from the plant."""

    def __init__(self, blocks):
        self.blocks = blocks
        self.reads = []

    def read(self, address, count):
        self.reads.append((address, count))
        if address not in self.blocks:
            raise AssertionError("unexpected read at %d" % address)
        return list(self.blocks[address])[:count]

    def close(self):
        pass


def site_with(grid_reg_w=0.0, dc_w=0.0, battery_reg_w=0.0, soc=50.0, w_sf=0,
              lifetime_wh=0, lifetime_sf=0):
    """A site whose registers say exactly what we tell them to.

    Canned blocks are laid out the way the wire carries them now - three small windows
    (inverter, meter, vendor), each covering just what the decoder touches - so these
    vectors double as the test that the windows are read from the right places.
    """
    meter = [0] * METER_LEN
    meter[20] = u16(w_sf)                                   # M_AC_Power_SF
    meter[16] = u16(int(round(grid_reg_w / 10.0 ** w_sf)))  # M_AC_Power (export positive)
    inv = [0] * INV_LEN
    inv[13] = u16(0)                                        # W_SF
    inv[12] = u16(int(round(0 / 10.0 ** 0)))                # inverter AC output
    inv[30] = u16(0)                                        # DC SF
    inv[29] = u16(int(round(dc_w)))                         # DC bus power
    # Lifetime AC energy (SunSpec 101 WH): acc32 high word first, then its scale factor.
    inv[INV_WH_OFF] = u16((lifetime_wh >> 16) & 0xFFFF)
    inv[INV_WH_OFF + 1] = u16(lifetime_wh & 0xFFFF)
    inv[INV_WH_SF_OFF] = u16(lifetime_sf)

    vendor = [0] * VENDOR_COUNT
    vendor[BAT_OFF:BAT_OFF + 2] = reg_words(battery_reg_w)
    vendor[SOC_OFF:SOC_OFF + 2] = reg_words(soc)

    blocks = {INV_START: inv[:INV_COUNT],
              METER_START: meter[:METER_COUNT],
              VENDOR_START: vendor}
    site = SolarEdgeSite(host="127.0.0.1", port=1503)
    site.client = FakeClient(blocks)
    return site


print("battery: register says discharging (negative) -> app reports discharging (positive)")
st = site_with(battery_reg_w=-763.0, dc_w=692.8).read()
check("register -763.0 W -> battery_power_w +763.0 (discharging)",
      abs(st.battery_power_w - 763.0) < 0.01, "got %.1f" % st.battery_power_w)
check("battery_discharging_w is the 763 W", abs(st.battery_discharging_w - 763.0) < 0.01,
      "got %.1f" % st.battery_discharging_w)
check("battery_charging_w is 0", abs(st.battery_charging_w) < 0.01, "got %.1f" % st.battery_charging_w)

print("battery: register says charging (positive) -> app reports charging (negative)")
st = site_with(battery_reg_w=500.0).read()
check("register +500.0 W -> battery_power_w -500.0 (charging)",
      abs(st.battery_power_w + 500.0) < 0.01, "got %.1f" % st.battery_power_w)
check("battery_charging_w is the 500 W", abs(st.battery_charging_w - 500.0) < 0.01,
      "got %.1f" % st.battery_charging_w)
check("battery_discharging_w is 0", abs(st.battery_discharging_w) < 0.01,
      "got %.1f" % st.battery_discharging_w)

print("soc passes through")
st = site_with(soc=88.1).read()
check("soc 88.1 %", st.battery_soc is not None and abs(st.battery_soc - 88.1) < 0.1,
      "got %s" % st.battery_soc)

print("grid: register says exporting (negative) -> app reports importing (positive)")
st = site_with(grid_reg_w=-1000.0).read()
check("register -1000 W -> grid_power_w +1000 (import)",
      abs(st.grid_power_w - 1000.0) < 0.01, "got %.1f" % st.grid_power_w)
check("importing_w is 1000 W", abs(st.importing_w - 1000.0) < 0.01, "got %.1f" % st.importing_w)
st = site_with(grid_reg_w=+1500.0).read()
check("register +1500 W -> grid_power_w -1500 (export)",
      abs(st.grid_power_w + 1500.0) < 0.01, "got %.1f" % st.grid_power_w)
check("exporting_w is 1500 W", abs(st.exporting_w - 1500.0) < 0.01, "got %.1f" % st.exporting_w)

print("pv: the DC register is the BUS (array + battery), so array = bus + battery(register)")
st = site_with(dc_w=692.0, battery_reg_w=-763.0).read()
check("array at ~0 W while the battery covers the load (bus 692, batt -763)",
      abs(st.pv_power_w - (-71.0)) < 0.2, "got %.1f" % st.pv_power_w)
st = site_with(dc_w=4000.0, battery_reg_w=-1000.0).read()
check("array producing 3000 W while the battery discharges 1000 W",
      abs(st.pv_power_w - 3000.0) < 0.2, "got %.1f" % st.pv_power_w)
st = site_with(dc_w=4000.0, battery_reg_w=+1000.0).read()
check("array producing 5000 W while 1000 W goes into the battery",
      abs(st.pv_power_w - 5000.0) < 0.2, "got %.1f" % st.pv_power_w)

print("request pattern: three small windows per cycle, like the openHAB reference")
site = site_with(grid_reg_w=-1000.0, battery_reg_w=-763.0, dc_w=692.8, soc=88.1)
site.read()
reads = list(site.client.reads)
check("exactly three reads per cycle", len(reads) == 3, str(reads))
check("inverter window = 40071 for 32 registers", reads[0] == (INV_START, INV_COUNT),
      str(reads[0]))
check("meter window = 40190 for 53 registers", reads[1] == (METER_START, METER_COUNT),
      str(reads[1]))
check("vendor window = 0xE174 for 18 registers", reads[2] == (VENDOR_START, VENDOR_COUNT),
      str(reads[2]))
check("no block exceeds the proxy's 125-register limit",
      all(count <= 125 for _, count in reads), str(reads))
check("total registers per cycle stay near the reference pattern (76 there, 103 here)",
      sum(count for _, count in reads) <= 110, str(sum(count for _, count in reads)))
check("no read touches the unused span between the models (40111..40189)",
      not any(start <= 40189 and start + count > 40111 for start, count in reads),
      str(reads))

print("lifetime AC energy: the inverter's own production counter, from the window we read anyway")
# The vector is a live reading of this plant, taken before the decode existed here.
st = site_with(lifetime_wh=29267336, lifetime_sf=0).read()
check("WH 29267336 with SF 0 -> 29267.336 kWh", abs(st.inverter_energy_kwh - 29267.336) < 0.001,
      "got %s" % st.inverter_energy_kwh)
st = site_with(lifetime_wh=2926734, lifetime_sf=1).read()
check("the scale factor is applied (SF 1 => a count is a tenth of a kWh)",
      abs(st.inverter_energy_kwh - 29267.34) < 0.01, "got %s" % st.inverter_energy_kwh)
st = site_with(lifetime_wh=0).read()
check("a zero register is 'no reading', never 0.0 kWh", st.inverter_energy_kwh is None,
      "got %r" % (st.inverter_energy_kwh,))
_site = site_with(lifetime_wh=29267336)
_site.read()
check("it stays at three window reads - the counter costs no extra Modbus traffic",
      len(_site.client.reads) == 3 and sum(c for _, c in _site.client.reads) == 103,
      str(_site.client.reads))

print("the driver is read-only: no path from this app into the inverter")
# The owner's rule (\"Ich will nicht Akku steuern\") is a property of the code, so it is
# pinned here structurally: the Modbus client has no write method, the site driver has
# nothing that steers the battery, and the control/limit register addresses must not
# reappear in the *code* (the module docstring may name them, to explain why they are gone).
import inspect  # noqa: E402
from evcharge.drivers.modbus import ModbusClient  # noqa: E402
from evcharge.drivers import solaredge as se_mod  # noqa: E402

modbus_writers = [n for n in dir(ModbusClient) if "write" in n.lower()]
check("the Modbus client exposes no write method at all", modbus_writers == [],
      str(modbus_writers))
steering = [n for n in dir(se_mod.SolarEdgeSite)
            if n.startswith("set_") or "battery_mode" in n or "discharge_limit" in n]
check("the site driver exposes nothing that steers the battery", steering == [], str(steering))
check("...and its read window count is unchanged (103 registers per cycle)",
      sum(count for _, count in reads) == 103, str(sum(count for _, count in reads)))

body = inspect.getsource(se_mod).split('"""', 2)[-1]      # code without the module docstring
for banned in ("write_single", "write_multiple", "V_CTRL", "V_EXPORT",
               "0xE00D", "0xE010", "0xE000", "0xE001", "0xE002"):
    check("...and %-12s does not appear in the driver code" % banned, banned not in body)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all decode-convention checks PASS")