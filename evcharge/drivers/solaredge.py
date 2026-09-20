"""SolarEdge site driver - reads through the local muxproxy (never the inverter).

The proxy is the single Modbus client for the inverter; this driver just reads
cached registers from it. Signed power convention used here:

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
METER_START, METER_COUNT = 40190, 53      # ... through M_Energy_W_SF at index 52
V_BAT_POWER, V_BAT_SOC = 0xE174, 0xE184
VENDOR_START, VENDOR_COUNT = 0xE174, 18   # battery power .. SOC in one window
V_CTRL_MODE, V_CTRL_DISCHARGE = 0xE00D, 0xE010
V_EXPORT_MODE, V_EXPORT_LIMIT_MODE, V_EXPORT_LIMIT_W = 0xE000, 0xE001, 0xE002
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
        st.raw = {"meter": meter, "inverter": inv, "vendor": v, "bat": bat, "soc": soc}
        return st

    # ---- battery control (optional, used by 'battery hold' strategy) -------
    def set_battery_discharge_limit(self, watts: float) -> None:
        regs = struct.pack(">f", float(watts))
        high, low = struct.unpack(">HH", regs)
        self.client.write_multiple(V_CTRL_DISCHARGE, [low, high])   # word-swapped

    def set_battery_mode(self, mode: int) -> None:
        """7 = maximize self-consumption, 3 = charge from PV+AC, 0 = off."""
        self.client.write_single(V_CTRL_MODE, int(mode))
