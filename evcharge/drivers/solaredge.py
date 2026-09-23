"""SolarEdge site driver - reads through the local muxproxy (never the inverter).

The proxy is the single Modbus client for the inverter; this driver just reads
cached registers from it. **It is read-only, deliberately and provably:**

* the inverter owns the house battery, and the owner's rule is that this app must
  not touch it ("Ich will nicht Akku steuern") - so the battery-control calls that
  once sat here (storage mode 0xE00D, discharge limit 0xE010) and the export-limit
  constants (0xE000..0xE002) are gone, together with the Modbus write primitives;
* `tests/test_solaredge_decode.py` pins that structurally - no write method on the
  client, and the control register addresses must not appear in this source again;
* the live canary is the proxy's own counter: `upstream_writes` must stay 0, and
  `proxy.classify()` turns any value above 0 into a *bad* card ("PROXY WROTE TO THE
  DEVICE"). Three days and 59k poll cycles of this plant: 0 writes.

If a future version ever needs to steer the battery, that is a deliberate feature -
with a watchdog that can never leave the storage in a special mode - not a helper
left lying in a driver.

Signed power convention used here:

    pv_power_w      >= 0   production
    grid_power_w    >0 import, <0 export      (convention used throughout this app)
    battery_power_w <0 charging, >0 discharging
    battery_soc     0..100
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .modbus import ModbusClient

METER_HDR, METER_DATA, METER_LEN = 40188, 40190, 105
INV_HDR, INV_DATA, INV_LEN = 40069, 40071, 50
# Small window reads, not whole models. The reference config for this class of inverter
# (openHAB: two pollers, 40 and 36 registers, refresh=10000, never a register at a time)
# reads only the windows it decodes and skips the unused span between the models.
# Same here: three reads, each covering exactly what the decoder below touches, and no
# read anywhere else. 103 registers per cycle in total.
INV_START, INV_COUNT = 40071, 32          # ... through DC power at index 30
# The inverter's lifetime AC energy (SunSpec model 101: WH as acc32 at offset 22, its scale
# factor at 24) lives inside that same window, so a day's production figure costs no extra
# Modbus traffic. Measured on this plant: 29,267.336 kWh with WH_SF 0, and the neighbouring
# registers decode consistently (offset 12/13 = the AC power this driver already uses).
INV_WH_OFF, INV_WH_SF_OFF = 22, 24
METER_START, METER_COUNT = 40190, 53      # ... through M_Energy_W_SF at index 52
V_BAT_POWER, V_BAT_SOC = 0xE174, 0xE184
VENDOR_START, VENDOR_COUNT = 0xE174, 18   # battery power .. SOC in one window
BAT_OFF = V_BAT_POWER - VENDOR_START      # 0
SOC_OFF = V_BAT_SOC - VENDOR_START        # 16


def s16(v: int) -> int:
    return v - 65536 if v > 32767 else v


def sf(v: int) -> int:
    v = s16(v)
    return 0 if v == -32768 or abs(v) > 10 else v


def f32_swapped(hi: int, lo: int) -> float:
    return struct.unpack(">f", struct.pack(">HH", lo, hi))[0]


def acc32(hi: int, lo: int) -> int:
    v = (hi << 16) | lo
    return 0 if v >= 0xFFFFFFFE else v


@dataclass
class SiteState:
    pv_power_w: float = 0.0
    inverter_ac_w: float = 0.0
    grid_power_w: float = 0.0
    grid_phase_w: List[float] = field(default_factory=list)
    grid_current_a: List[float] = field(default_factory=list)
    grid_voltage_v: List[float] = field(default_factory=list)
    grid_hz: float = 0.0
    battery_power_w: float = 0.0
    battery_soc: Optional[float] = None
    battery_capacity_kwh: float = 10.0
    grid_import_kwh: Optional[float] = None
    grid_export_kwh: Optional[float] = None
    # The inverter's own lifetime AC energy. The owner's monitoring app calls this production,
    # and it is what a daily figure must be compared against - an integral of power samples is
    # not, because it loses whatever happens while the app is not running.
    inverter_energy_kwh: Optional[float] = None
    raw: Dict = field(default_factory=dict)

    @property
    def battery_charging_w(self) -> float:
        return max(0.0, -self.battery_power_w)

    @property
    def battery_discharging_w(self) -> float:
        return max(0.0, self.battery_power_w)

    @property
    def exporting_w(self) -> float:
        return max(0.0, -self.grid_power_w)

    @property
    def importing_w(self) -> float:
        return max(0.0, self.grid_power_w)


class SolarEdgeSite:
    def __init__(self, host: str = "127.0.0.1", port: int = 1503, unit: int = 1,
                 battery_capacity_kwh: float = 10.0):
        self.client = ModbusClient(host, port, unit)
        self.battery_capacity_kwh = battery_capacity_kwh

    def close(self) -> None:
        self.client.close()

    def read(self) -> SiteState:
        # Exactly three window reads, nothing else - no per-register reads, no reads
        # outside these spans, no reading of the span between the models. The request
        # pattern the inverter sees is fixed, small and known (see the constants above).
        inv = self.client.read(INV_START, INV_COUNT)
        meter = self.client.read(METER_START, METER_COUNT)
        v = self.client.read(VENDOR_START, VENDOR_COUNT)
        for name, got, want in (("inverter", inv, INV_COUNT), ("meter", meter, METER_COUNT),
                                ("vendor", v, VENDOR_COUNT)):
            if len(got) < want:
                raise IOError("short register block %s: %d of %d registers"
                              % (name, len(got), want))
        bat = v[BAT_OFF:BAT_OFF + 2]
        soc = v[SOC_OFF:SOC_OFF + 2]

        w_sf, a_sf, v_sf = sf(meter[20]), sf(meter[4]), sf(meter[13])
        hz_sf, e_sf = sf(meter[15]), sf(meter[52])
        grid_raw = s16(meter[16]) * 10.0 ** w_sf

        st = SiteState()
        st.grid_power_w = -grid_raw                      # import positive
        st.grid_phase_w = [s16(meter[i]) * 10.0 ** w_sf * -1 for i in (17, 18, 19)]
        st.grid_current_a = [s16(meter[i]) * 10.0 ** a_sf for i in (1, 2, 3)]
        st.grid_voltage_v = [s16(meter[i]) * 10.0 ** v_sf for i in (5, 6, 7)]
        st.grid_hz = s16(meter[14]) * 10.0 ** hz_sf
        st.inverter_ac_w = s16(inv[12]) * 10.0 ** sf(inv[13])
        pv_dc = s16(inv[29]) * 10.0 ** sf(inv[30])
        bat_w = f32_swapped(*bat)                        # register convention: + = charging
        st.battery_power_w = -bat_w                      # app convention: + = discharging
        st.battery_soc = None if bat_w != bat_w else f32_swapped(*soc)
        # The vendor DC register is the inverter's DC BUS, which carries the battery
        # as well as the array, so the array's own production is bus + battery
        # (register convention, i.e. adding the negative discharge). Verified against
        # live registers: dc 692.8 W + battery -763.0 W -> -70.2 W = array at ~0.
        st.pv_power_w = (pv_dc + bat_w) if pv_dc == pv_dc and bat_w == bat_w else pv_dc
        st.battery_capacity_kwh = self.battery_capacity_kwh
        imp, exp = acc32(meter[44], meter[45]), acc32(meter[36], meter[37])
        st.grid_import_kwh = round(imp * 10.0 ** (e_sf - 3), 3) if imp else None
        st.grid_export_kwh = round(exp * 10.0 ** (e_sf - 3), 3) if exp else None
        # Lifetime AC energy, from the same window (SunSpec 101 WH, acc32 with its SF). A zero
        # read is treated as no reading, not as 0 kWh - a register that briefly reads 0 after a
        # power-up would otherwise look like a counter reset and poison every daily delta.
        wh = acc32(inv[INV_WH_OFF], inv[INV_WH_OFF + 1])
        wh_sf = s16(inv[INV_WH_SF_OFF])
        st.inverter_energy_kwh = round(wh * 10.0 ** (wh_sf - 3), 3) if wh else None
        st.raw = {"meter": meter, "inverter": inv, "vendor": v, "bat": bat, "soc": soc}
        return st
