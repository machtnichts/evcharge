"""PV forecast for the roof array - observation only, no control (step 1).

The problem this serves: the house battery (DC-coupled to the SolarEdge array) fills by
10-11:00 in summer, and after that the flexible consumer that could still take the midday
sun is the car. Which of the two should get the surplus is a *weather* question - "will the
battery still fill in time if the car takes it now?" - so the controller will need a
forecast of the rest of the day. This module produces that forecast and, more importantly,
collects the evidence to judge it:

* hourly irradiance for the array's own planes (4 kWp east + 4 kWp west, unshaded) from
  Open-Meteo - free, no key, `global_tilted_irradiance` for a given tilt/azimuth;
* the *expected* yield per hour (`GTI x kWp` for DC, times a performance ratio for AC),
  which is what the validation against the owner's SolarEdge days showed to be good to ~2 %
  on a clear day (22.09: predicted 27.9 kWh, measured 28.5) and ~27 % high on cloudy ones
  (20./21.09: 0.73 / 0.74) - so it is used with a margin and re-evaluated every cycle;
* `factor_today` = measured-so-far / predicted-so-far, the correction that makes a dull
  morning (measured 21.09: production flat at ~0.3 kW until 09:30) pull the rest of the day
  down with it. That is the number the rule will multiply the remaining forecast by.

**Nothing here steers anything.** The module has no actuator, its numbers are published for
display and written to a CSV once a day, and every failure path keeps the previous data and
only sets a `stale` flag - a cycle must never wait on the internet.

The garage PV (1.6 kWp, west, shaded) is deliberately absent: the battery is DC-coupled to
the roof array, so the garage's AC-coupled energy can never charge it.

Pure arithmetic is separated from the network so it can be tested without one
(`tests/test_pv_forecast.py` injects a `fetcher`).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

LOG = logging.getLogger("evcharge.forecast")

API = "https://api.open-meteo.com/v1/forecast"
# The second opinion. A different model, also keyless, and deliberately only for the day total:
# if the two disagree, the truth tomorrow decides which one to believe.
ALT_API = "https://api.forecast.solar"
# Sane bounds for the correction: a factor outside this came from a broken measurement or a
# broken forecast, and applying it would do more harm than ignoring it.
FACTOR_MIN, FACTOR_MAX = 0.25, 1.60
# Below this the day simply has not started (a few watt-seconds of sun at 06:30 are not
# evidence and must not be logged as an anomaly); above it, a wildly out-of-range factor is
# worth a warning.
MIN_MEASURED_KWH = 0.05


@dataclass(frozen=True)
class Plane:
    """One roof plane. Azimuth in Open-Meteo's convention: 0 = south, -90 = east, 90 = west."""
    kwp: float
    azimuth: float
    tilt: float = 25.0


@dataclass
class Forecast:
    """Hourly estimates, keyed by local wall-clock hour ("YYYY-MM-DDTHH:00")."""
    fetched_at: float = 0.0
    dc_wh: Dict[str, float] = field(default_factory=dict)   # ideal array output per hour
    ac_wh: Dict[str, float] = field(default_factory=dict)   # after the performance ratio
    cloud_pct: Dict[str, float] = field(default_factory=dict)   # cloud cover per hour, %
    source: str = ""

    def clouds(self, date: str, h0: int = 7, h1: int = 19) -> Optional[float]:
        """Mean cloud cover over the day's productive hours - the number that explains why a
        day came out at 0.73 or 1.02 instead of being a separate mystery."""
        vals = [v for k, v in self.cloud_pct.items()
                if k.startswith(date) and h0 <= int(k[11:13]) < h1]
        return round(sum(vals) / len(vals), 1) if vals else None

    def _sum(self, series: Dict[str, float], date: str, upto: Optional[str] = None) -> float:
        total = 0.0
        for hour, wh in series.items():
            if not hour.startswith(date):
                continue
            if upto is not None and hour > upto:
                continue
            total += wh
        return total / 1000.0                                # Wh -> kWh

    def day_kwh(self, date: str, ac: bool = True) -> float:
        return self._sum(self.ac_wh if ac else self.dc_wh, date)

    def until_kwh(self, date: str, now_hour: str, ac: bool = True) -> float:
        """What the forecast expected for this day up to and including the current hour."""
        return self._sum(self.ac_wh if ac else self.dc_wh, date, upto=now_hour)

    def remaining_kwh(self, date: str, now_hour: str, ac: bool = True) -> float:
        """From the next hour to the end of the day - night hours contribute nothing, so no
        sunrise/sunset arithmetic is needed here."""
        series = self.ac_wh if ac else self.dc_wh
        total = 0.0
        for hour, wh in series.items():
            if hour.startswith(date) and hour > now_hour:
                total += wh
        return total / 1000.0


def parse_hour(iso: str) -> str:
    """Open-Meteo returns "2026-09-23T13:00" - keep exactly that shape as the key."""
    return iso[:13] + ":00"


def build(data: dict, planes: List[Plane], pr: float, clouds: Optional[Dict[str, float]] = None) -> Forecast:
    """Turn one Open-Meteo answer into hourly Wh for the whole array. Pure."""
    h = (data or {}).get("hourly") or {}
    times = h.get("time") or []
    gti = h.get("global_tilted_irradiance") or []
    if len(times) != len(gti):
        raise ValueError("hourly arrays differ in length (%d vs %d)" % (len(times), len(gti)))
    cc = h.get("cloud_cover") or []
    out = Forecast(source="open-meteo")
    for i, (t, v) in enumerate(zip(times, gti)):
        k = parse_hour(t)
        out.dc_wh[k] = float(v or 0.0) * planes_kwp(planes)      # Wh: W/m2 * kWp
        out.ac_wh[k] = out.dc_wh[k] * pr
        if i < len(cc) and cc[i] is not None:
            out.cloud_pct[k] = float(cc[i])
    if clouds:
        out.cloud_pct = clouds
    return out


def parse_alt(data: dict, day: str) -> Optional[float]:
    """forecast.solar's day total in kWh for one plane, or None if it did not answer for that
    day - the second opinion must never look like a number when it is not there."""
    try:
        wh = (data or {}).get("result", {}).get("watt_hours_day", {}) or {}
        val = wh.get(day)
        return None if val is None else round(float(val) / 1000.0, 3)
    except (TypeError, ValueError, AttributeError):
        return None


def planes_kwp(planes: List[Plane]) -> float:
    return sum(p.kwp for p in planes)


def factor(measured_kwh: Optional[float], expected_kwh: Optional[float]) -> Optional[float]:
    """measured / expected for the same window, clamped, or None when there is nothing to
    compare.

    Returns None (never 0.0) when the measurement is flat zero: before sunrise, a broken site
    reading and a day with genuinely no sun all look identical from here, so the honest answer
    is "no basis" - the caller then falls back to its sun-relative rule instead of trusting a
    factor that a dead sensor could have produced.
    """
    if measured_kwh is None or expected_kwh is None or expected_kwh < 0.3:
        return None
    if measured_kwh < MIN_MEASURED_KWH:
        return None                    # silent: the day has not started, that is not an anomaly
    f = measured_kwh / expected_kwh
    if f < FACTOR_MIN or f > FACTOR_MAX:
        LOG.warning("PV factor out of bounds (%.2f: measured %.2f kWh vs expected %.2f kWh) - "
                    "ignored", f, measured_kwh, expected_kwh)
        return None
    return round(f, 3)


class PvForecast:
    """Fetches on its own cadence, keeps the last good answer, and never raises."""

    def __init__(self, planes: List[Plane], lat: float, lon: float, pr: float = 0.85,
                 tz: str = "Europe/Berlin", every_s: float = 3600.0,
                 cache_path: str = "logs/pv_forecast.json", timeout: float = 20.0,
                 enabled: bool = True, fetcher: Optional[Callable[[str], dict]] = None,
                 now_fn: Callable[[], float] = time.time,
                 local_now: Optional[Callable[[], str]] = None,
                 alt_enabled: bool = True):
        self.planes, self.lat, self.lon, self.pr = planes, lat, lon, pr
        self.tz, self.every_s, self.timeout = tz, max(60.0, float(every_s)), timeout
        self.cache_path = cache_path
        self.enabled = bool(enabled)
        self.alt_enabled = bool(alt_enabled)
        self._fetch = fetcher or self._http
        self.now = now_fn
        self._local_now = local_now or self._zone_now
        self.data: Optional[Forecast] = None
        self.fetched_at = 0.0
        self.failures = 0
        self.fetches = 0
        self.last_error = ""
        self.stale = True
        self._warned = False
        # The second opinion (forecast.solar, a different model, also keyless). Kept in its own
        # corner: it must never be able to break the forecast the app already has.
        self.alt_day_kwh: Optional[float] = None
        self.alt_fetched_at = 0.0
        self.alt_error = ""
        self._alt_warned = False
        self._load_cache()

    # -- time ------------------------------------------------------------
    def _zone_now(self) -> str:
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(self.tz)).strftime("%Y-%m-%dT%H:00")
        except Exception:  # noqa: BLE001 - an unknown zone must not stop the app
            return datetime.utcnow().strftime("%Y-%m-%dT%H:00")

    @property
    def hour_now(self) -> str:
        return self._local_now()

    # -- network ---------------------------------------------------------
    def _url(self, plane: Plane, clouds: bool = False) -> str:
        hourly = "global_tilted_irradiance,cloud_cover" if clouds else "global_tilted_irradiance"
        q = {"latitude": "%.4f" % self.lat, "longitude": "%.4f" % self.lon,
             "hourly": hourly, "tilt": "%d" % int(plane.tilt),
             "azimuth": "%d" % int(plane.azimuth), "past_days": "1", "forecast_days": "2",
             "timezone": self.tz}
        return API + "?" + urllib.parse.urlencode(q)

    def _alt_url(self, plane: Plane) -> str:
        return "%s/estimate/%.4f/%.4f/%d/%d/%s?resolution=60" % (
            ALT_API, self.lat, self.lon, int(plane.tilt), int(plane.azimuth), plane.kwp)

    def _fetch_alt(self, day: str) -> None:
        """Ask forecast.solar for the same day, and keep the answer strictly separate: a second
        opinion that can break the first one is worse than no second opinion."""
        total = 0.0
        for plane in self.planes:
            answer = parse_alt(self._fetch(self._alt_url(plane)), day)
            if answer is None:
                raise ValueError("no day total for %s" % day)
            total += answer
        self.alt_day_kwh = round(total, 3)
        self.alt_fetched_at = self.now()
        self.alt_error = ""
        self._alt_warned = False
        LOG.info("second opinion (forecast.solar) for %s: %.2f kWh", day, self.alt_day_kwh)

    def _fetch_alt_quietly(self, day: str) -> None:
        if not self.alt_enabled:
            return
        if self.alt_day_kwh is not None and (self.now() - self.alt_fetched_at) < self.every_s:
            return
        try:
            self._fetch_alt(day)
        except Exception as exc:  # noqa: BLE001 - never let the second opinion break the first
            self.alt_error = "%s: %s" % (type(exc).__name__, exc)
            if not self._alt_warned:
                self._alt_warned = True
                LOG.warning("second opinion (forecast.solar) unavailable: %s", exc)

    def _http(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "evcharge/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as fh:
            return json.load(fh)

    def update(self, force: bool = False) -> None:
        """Refresh when the cadence says so. Any failure keeps the previous data."""
        if not self.enabled:
            return
        day = self.hour_now[:10]
        if not force and self.data is not None and (self.now() - self.fetched_at) < self.every_s:
            self.stale = self._hours_old() > 3.0
            self._fetch_alt_quietly(day)          # own cadence, independent of the primary
            return
        merged = Forecast(source="open-meteo")
        try:
            for idx, plane in enumerate(self.planes):
                # build() with this plane's own kWp and the PR: dc = GTI x kWp, ac = x PR
                answer = build(self._fetch(self._url(plane, clouds=(idx == 0))), [plane], self.pr)
                for k, wh in answer.dc_wh.items():
                    merged.dc_wh[k] = merged.dc_wh.get(k, 0.0) + wh
                for k, wh in answer.ac_wh.items():
                    merged.ac_wh[k] = merged.ac_wh.get(k, 0.0) + wh
                if idx == 0:
                    # Cloud cover describes the place, not a plane - one request is enough.
                    merged.cloud_pct = dict(answer.cloud_pct)
        except Exception as exc:  # noqa: BLE001 - the app must never wait on the internet
            self.failures += 1
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self.stale = True
            if not self._warned:
                self._warned = True
                LOG.warning("PV forecast fetch failed (%s) - keeping the previous data", exc)
            return
        merged.fetched_at = self.now()
        self.data, self.fetched_at = merged, merged.fetched_at
        self.fetches += 1
        self.failures = 0
        self.last_error = ""
        self.stale = False
        self._warned = False
        self._save_cache()
        LOG.info("PV forecast updated: today %.1f kWh, rest of today %.1f kWh",
                 merged.day_kwh(self.hour_now[:10]),
                 merged.remaining_kwh(self.hour_now[:10], self.hour_now))
        self._fetch_alt_quietly(day)

    def _hours_old(self) -> float:
        return 0.0 if not self.fetched_at else (self.now() - self.fetched_at) / 3600.0

    # -- cache -----------------------------------------------------------
    def _load_cache(self) -> None:
        """A restart must not blank the display - read back the last good answer."""
        try:
            path = os.path.expanduser(self.cache_path)
            if not os.path.exists(path):
                return
            with open(path) as fh:
                blob = json.load(fh)
            f = Forecast(fetched_at=float(blob.get("fetched_at", 0.0)),
                         dc_wh={k: float(v) for k, v in (blob.get("dc_wh") or {}).items()},
                         ac_wh={k: float(v) for k, v in (blob.get("ac_wh") or {}).items()},
                         cloud_pct={k: float(v) for k, v in (blob.get("cloud_pct") or {}).items()},
                         source=str(blob.get("source", "cache")))
            if f.ac_wh:
                self.data, self.fetched_at, self.stale = f, f.fetched_at, True
        except Exception as exc:  # noqa: BLE001
            LOG.warning("could not read the forecast cache %s: %s", self.cache_path, exc)

    def _save_cache(self) -> None:
        try:
            path = os.path.expanduser(self.cache_path)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w") as fh:
                json.dump({"fetched_at": self.fetched_at, "source": "open-meteo",
                           "dc_wh": self.data.dc_wh if self.data else {},
                           "ac_wh": self.data.ac_wh if self.data else {},
                           "cloud_pct": self.data.cloud_pct if self.data else {}}, fh)
        except OSError as exc:
            LOG.warning("could not write the forecast cache %s: %s", self.cache_path, exc)

    # -- what the app shows ---------------------------------------------
    def as_state(self, measured_today_kwh: Optional[float] = None,
                 soc: Optional[float] = None, soc_target: Optional[float] = None,
                 capacity_kwh: Optional[float] = None,
                 house_rest_kwh: Optional[float] = None,
                 margin: float = 1.3) -> Dict:
        """Everything the UI needs - numbers only, no verdict that could be mistaken for a
        control decision (step 1: the rule is not wired, this is evidence gathering)."""
        hour = self.hour_now
        date = hour[:10]
        out: Dict = {"enabled": self.enabled, "stale": self.stale, "fetches": self.fetches,
                     "failures": self.failures, "last_error": self.last_error,
                     "age_min": round(self._hours_old() * 60.0, 1), "hour": hour,
                     "planes": [{"kwp": p.kwp, "azimuth": p.azimuth, "tilt": p.tilt}
                                for p in self.planes],
                     "pr": self.pr}
        if self.data is None:
            out["note"] = "no forecast yet"
            return out
        out["today_kwh"] = round(self.data.day_kwh(date), 2)
        out["expected_so_far_kwh"] = round(self.data.until_kwh(date, hour), 2)
        out["remaining_kwh"] = round(self.data.remaining_kwh(date, hour), 2)
        out["cloud_cover_pct"] = self.data.clouds(date)
        out["alt_today_kwh"] = self.alt_day_kwh
        out["alt_error"] = self.alt_error
        out["alt_age_min"] = (None if not self.alt_fetched_at
                              else round((self.now() - self.alt_fetched_at) / 60.0, 1))
        out["measured_kwh"] = None if measured_today_kwh is None else round(measured_today_kwh, 2)
        out["factor_today"] = factor(measured_today_kwh, out["expected_so_far_kwh"])
        if out["factor_today"] is not None and self.data is not None:
            out["remaining_corrected_kwh"] = round(
                self.data.remaining_kwh(date, hour) * out["factor_today"], 2)
        need = None
        if None not in (soc, soc_target, capacity_kwh):
            need = max(0.0, (float(soc_target) - float(soc)) / 100.0 * float(capacity_kwh))
            out["battery_need_kwh"] = round(need, 2)
        # Published so the tooltip can show every number the shadow verdict used - a displayed
        # verdict that hides its inputs invites being mistaken for a decision.
        out["soc_target"], out["soc"] = soc_target, soc
        out["capacity_kwh"], out["margin"] = capacity_kwh, margin
        out["house_rest_kwh"] = house_rest_kwh
        if need is not None and out.get("remaining_corrected_kwh") is not None:
            supply = out["remaining_corrected_kwh"] - float(house_rest_kwh or 0.0)
            out["supply_after_house_kwh"] = round(supply, 2)
            out["would_be"] = "battery" if supply < need * margin else "car"
            out["would_be_text"] = ("Akku-Vorrang (Rest reicht nicht)" if supply < need * margin
                                    else "Auto-Vorrang (Rest reicht fuer den Akku)")
        return out
