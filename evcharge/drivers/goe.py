"""go-e Charger driver (HTTP API v1, firmware 041.x).

Field layout verified empirically against this box (and against the readings of the
controller that ran here before):

    nrg[0..2]  phase voltages (V)
    nrg[3]     constant (1 on this box, also at 0 A) - not a current
    nrg[4..6]  phase currents (0.1 A)
    nrg[7..9]  phase power    (0.1 kW)
    nrg[11]    total power    (10 W)          -> 411 == 4110 W
    nrg[12]    power factor   (0.01)
    eto        lifetime energy (0.1 kWh)      -> 200370 == 20037.0 kWh

Reads use /status. Writes use the v1 setter form, verified on this device:

    /mqtt?payload=amx=<6..32>      set the current limit  (see below)
    /mqtt?payload=alw=<0|1>        allow / deny charging  (the start/stop key)

A local setter answers with the charger's full status JSON (not the cloud API's
{"success":true,...}); a bad payload answers {"success":false,"error":...}. /api/set
(HTTP API v2) and /set (legacy) are tried as fallbacks - both return HTTP 404 here.

Which key sets what, measured on this box:
  * amx  - the working current key. Writing amx=14 took effect (the car went from 7.9 A
           to 13.7 A) and /status then reported amp=14, so `amp` carries the *effective*
           limit and is readable. Per go-e's spec amx is not written to flash, which is
           why PV charging (constant small changes) must use it and never amp.
  * alw  - the working start/stop key (documented as settable and readable, so the
           write can be verified by reading it back).
  * frc  - NOT settable on this firmware: the box answers HTTP 200 with its status
           dump while silently ignoring it. Trusting that 200 is how a controller ends
           up believing it started a charge that never happened.

Phases must be counted from the *currents*, never from `pha`. pha is a bit field

    0b00ABCDEF   A/B/C = phase 3/2/1 in front of the contactor
                 D/E/F = phase 3/2/1 behind the contactor

but it describes the box's contactor wiring, not the vehicle: this box reports 63
(all six bits) while the car draws on a single phase ([0.1, 7.9, 0.0], 1830 W total).
A 3-phase wallbox with a 1-phase vehicle cable is exactly this case, and a controller
that trusts the phase count waits for 3x the real minimum power.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

CAR_STATES = {1: "idle", 2: "charging", 3: "waiting", 4: "complete", 5: "error"}

# Write forms, best first: (path, params_must_be_url_encoded_as_one_payload).
# This box speaks the first one; the others exist for firmware generations where
# /mqtt is absent.
WRITE_FORMS = (
    ("/mqtt?payload=", True),   # HTTP API v1 - verified on this device
    ("/api/set?", False),       # HTTP API v2 (needs the API enabled in the app)
    ("/set?", False),           # legacy v1 short form
)


@dataclass
class ChargerState:
    connected: bool = False
    charging: bool = False
    car_state: str = "unknown"
    enabled: bool = False            # alw: 1 = charging allowed
    max_current: int = 0             # effective limit in A, as reported in `amp`
    phases: int = 0                  # phases carrying current, 0 = nothing flowing
    error: int = 0
    voltages: List[float] = field(default_factory=list)
    currents: List[float] = field(default_factory=list)
    power_per_phase: List[float] = field(default_factory=list)
    power: float = 0.0
    power_factor: float = 0.0
    energy_total_kwh: float = 0.0
    temperatures: List[float] = field(default_factory=list)
    firmware: str = ""
    raw: Dict = field(default_factory=dict)

    @property
    def energy_kwh(self) -> float:
        return self.energy_total_kwh


class GoEClient:
    def __init__(self, host: str, timeout: float = 5.0, phase: int = 1):
        self.base = "http://%s" % host
        self.timeout = timeout
        # phase index (1-based) of the phase this vehicle charges on
        self.phase = max(1, min(3, phase))
        self._set_path: Optional[str] = None   # learned write endpoint

    def _get(self, path: str) -> str:
        with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
            return r.read().decode("utf-8", "replace").strip()

    def status(self) -> ChargerState:
        data = json.loads(self._get("/status"))
        nrg = data.get("nrg") or []
        st = ChargerState()
        st.raw = data
        st.firmware = str(data.get("fwv", ""))
        try:
            st.error = int(data.get("err", 0))
        except (TypeError, ValueError):
            st.error = -1
        car = str(data.get("car", ""))
        st.car_state = CAR_STATES.get(int(car) if car.isdigit() else 0, "unknown")
        st.charging = car == "2"
        st.connected = car in ("2", "3", "4")
        st.enabled = str(data.get("alw", "0")) == "1"
        try:
            st.max_current = int(data.get("amp", 0))
        except (TypeError, ValueError):
            st.max_current = 0
        if len(nrg) >= 3:
            st.voltages = [float(x) for x in nrg[0:3]]
        # MEASURED layout on this box (firmware 041.0), captured while the car drew 6 A on
        # three phases:
        #     [0..2]   228 226 227    volts
        #     [3]      1              a constant, present at 0 A too - NOT a current
        #     [4..6]   60 59 59       per-phase currents, 0.1 A   (6.0, 5.9, 5.9 A)
        #     [7..9]   13 13 13       per-phase power, 0.1 kW      (1.3 kW each)
        #     [11]     411            total power, 10 W            (4110 W)
        #     [12..14] 100 100 100    power factors, 0.01
        # The two blocks after the voltages sit one slot further along than the documented
        # layout, which is exactly how a three-phase charge was counted as two phases:
        # reading [3..5] as currents gives (0.1, 6.0, 5.9) A. The go-e app showed 1.3 kW
        # on every phase at that moment, which is what [7..9] reports.
        if len(nrg) >= 7:
            st.currents = [float(x) / 10.0 for x in nrg[4:7]]
        # phases the vehicle actually uses: count the phases carrying current (0.5 A
        # filters the ~0.1 A noise this box shows on idle channels). `pha` cannot be
        # used - it reports the contactor wiring, all three bits even on a 1-phase cable
        # (measured: pha=63 while only one phase carried current).
        st.phases = sum(1 for c in st.currents if c >= 0.5) if st.charging else 0
        if len(nrg) >= 10:
            st.power_per_phase = [float(x) / 10.0 for x in nrg[7:10]]
        if len(nrg) >= 12:
            st.power = float(nrg[11]) * 10.0
        if len(nrg) >= 13:
            st.power_factor = float(nrg[12]) / 100.0
        try:
            st.energy_total_kwh = float(data.get("eto", 0)) / 10.0
        except (TypeError, ValueError):
            st.energy_total_kwh = 0.0
        tma = data.get("tma")
        if isinstance(tma, list):
            st.temperatures = [float(x) for x in tma]
        return st

    # ---- control (only called when the controller has control enabled) ----
    def set_max_current(self, amps: float) -> bool:
        """Set the charging current limit. Uses amx: flash-free, see module docstring."""
        amps_i = int(round(max(6.0, min(32.0, amps))))
        return self._set({"amx": amps_i})

    def set_charging(self, want: bool) -> bool:
        """Allow/deny charging, verified by reading `alw` back.

        This box's start/stop key is alw - the documented settable "allow_charging"
        flag, and readable, so the write can be confirmed. `frc` is answered with 200
        and ignored, which is exactly why this verifies instead of trusting a 200.
        """
        target = 1 if want else 0
        tried = []
        for payload in ({"alw": target}, {"frc": 2 if want else 1}):
            key = next(iter(payload))
            try:
                self._set(payload)
            except IOError as exc:
                tried.append("%s -> %s" % (key, exc))
                continue
            time.sleep(1.0)          # give the box a moment before confirming
            try:
                now = int(self.status().raw.get("alw", -1))
            except Exception as exc:  # noqa: BLE001
                tried.append("%s -> readback failed: %s" % (key, exc))
                continue
            if now == target:
                return True
            tried.append("%s accepted but alw stayed %s" % (key, now))
        raise IOError("go-e cannot %s charging; tried: %s"
                      % ("start" if want else "stop", "; ".join(tried)))

    def set_neutral(self) -> bool:
        return self._set({"frc": 0})

    @staticmethod
    def _interpret(body: str, key: str):
        """Classify a setter response as (accepted, description)."""
        text = body.strip()
        if text.startswith("{"):
            try:
                resp = json.loads(text)
            except json.JSONDecodeError:
                return False, "unparseable response: %s" % text[:60]
            if isinstance(resp, dict):
                if resp.get("success") is False:
                    return False, str(resp.get("error") or "rejected")
                if resp.get("success") is True:
                    return True, "ok"
                # A status dump (this firmware's answer to a local set) is large and
                # carries charger state. Check it before the per-key envelope below,
                # because its key names collide with setters ("amp" is in there).
                # NOTE: a dump means "the box answered", not "the key was applied" -
                # it ignores unknown keys, so writes that matter must be verified by
                # reading the value back (see set_charging).
                if len(resp) > 5:
                    return True, "ok (full status response)"
                if key in resp:
                    value = resp[key]
                    if value is True or isinstance(value, (int, float)):
                        return True, "ok"
                    return False, "rejected %s: %s" % (key, value)
            return False, "unexpected response: %s" % text[:60]
        if "not found" in text.lower():
            return False, text[:60]
        return True, "ok (plain response)"

    def _set(self, params: Dict) -> bool:
        """Write parameters, returning True on success.

        Tries each write form in turn and remembers the one that works, reporting all of
        them when none does (the usual cause being the HTTP API switched off in the app).
        """
        key = next(iter(params))
        joined = "&".join("%s=%s" % (k, v) for k, v in params.items())
        forms = [f for f in WRITE_FORMS if self._set_path in (None, f[0])] or list(WRITE_FORMS)
        tried = []
        for path, as_payload in forms:
            # in the payload form the assignment has to survive as one query value
            url = path + (joined.replace("&", "%26") if as_payload else joined)
            try:
                body = self._get(url)
            except urllib.error.HTTPError as exc:
                tried.append("%s -> HTTP %s" % (path, exc.code))
                continue
            except urllib.error.URLError as exc:
                raise IOError("go-e unreachable: %s" % exc)
            accepted, description = self._interpret(body, key)
            if accepted:
                self._set_path = path
                return True
            tried.append("%s -> %s" % (path, description))
        raise IOError("go-e write failed (%s); is the HTTP API enabled in the "
                      "go-e app? tried: %s" % (key, "; ".join(tried)))

    def phase_current(self, st: ChargerState) -> float:
        """Current actually flowing into the vehicle.

        This go-e measures the single-phase vehicle load on its second channel
        (observed: [0.1, 7.9, 0.0] while charging at 7.9 A), so taking the
        configured phase blindly would read ~0 A. Use the largest phase current,
        which is correct for single-phase charging and for balanced 3-phase.
        """
        if not st.currents:
            return 0.0
        return max(st.currents)