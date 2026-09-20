"""Display-only reader for the Modbus proxy's /status, so the UI can show the health of
the link to the inverter.

The proxy is exactly where a failure hides: when it cannot reach the inverter it answers
clients from its cache, so a frozen meter looks like a healthy link - that is how a
657-minute-old reading once went unnoticed. Its /status endpoint tells the truth
(last successful upstream read, current backoff, error counters) and this module turns
that into a headline the web UI can show at a glance.

Stdlib only, and it never raises: a failed fetch is counted, not thrown.
"""
import json
import time
import urllib.request

# A good read older than this means the meter may be frozen behind the proxy.
AGE_WARN_S = 200.0
AGE_BAD_S = 600.0


def _f(value, default=0.0):
    """Anything at all -> float, never an exception (a status endpoint may be wrong)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def classify(stats, now=None):
    """Turn a proxy /status 'stats' object into a headline and a level. Pure."""
    now = time.time() if now is None else now
    if not stats:
        return {"level": "bad", "text": "PROXY UNREACHABLE", "ok_age_s": None}
    ok = _f(stats.get("last_upstream_ok"))
    age = None if ok <= 0 else max(0.0, float(now) - ok)
    backoff = _f(stats.get("upstream_backoff_s"))
    writes = int(_f(stats.get("upstream_writes")))
    if writes > 0:
        return {"level": "bad", "text": "PROXY WROTE TO THE DEVICE", "ok_age_s": age}
    if backoff > 0:
        return {"level": "warn", "text": "PROXY BACKING OFF %.0f s" % backoff,
                "ok_age_s": age}
    if age is None:
        return {"level": "warn", "text": "PROXY: NO GOOD READ YET", "ok_age_s": None}
    if age >= AGE_BAD_S:
        return {"level": "bad", "text": "PROXY STALE %.0f min" % (age / 60.0),
                "ok_age_s": age}
    if age >= AGE_WARN_S:
        return {"level": "warn", "text": "PROXY READ %.0f s OLD" % age, "ok_age_s": age}
    return {"level": "ok", "text": "PROXY OK", "ok_age_s": age}


def summary(stats, now=None, failures=0):
    """Everything the card shows, as plain JSON-ready values. Pure."""
    # Normalised here as well as in classify(): the last_error_age_s maths below uses
    # `now` directly, and callers that pass nothing must not get float(None) - that is
    # a crash per cycle whenever the proxy reports an error timestamp.
    now = time.time() if now is None else now
    out = classify(stats, now)
    s = stats or {}
    out.update({
        "failures": int(failures),
        "backoff_s": _f(s.get("upstream_backoff_s")),
        "errors": int(_f(s.get("upstream_errors"))),
        "reconnects": int(_f(s.get("upstream_reconnects"))),
        "timeouts": int(_f(s.get("upstream_timeouts"))),
        "reads": int(_f(s.get("upstream_reads"))),
        "writes": int(_f(s.get("upstream_writes"))),
        "validation_failures": int(_f(s.get("validation_failures"))),
        "requests": int(_f(s.get("requests_total"))),
        "hits": int(_f(s.get("requests_cache_hit"))),
        "misses": int(_f(s.get("requests_cache_miss"))),
        "clients_active": int(_f(s.get("clients_active"))),
        "uptime_s": _f(s.get("uptime_s")),
        "last_error": str(s.get("last_upstream_error") or ""),
    })
    # How long ago the last error happened, for "vor 15 min" in the UI. Exact times live
    # in the proxy's log; a rough age is what tells you whether it still matters.
    at = _f(s.get("last_upstream_error_at"))
    out["last_error_at"] = at or None
    out["last_error_age_s"] = None if at <= 0 else max(0.0, float(now) - at)
    return out


class ProxyStatusClient:
    """Fetches the proxy's /status. Failures are counted, never raised."""

    def __init__(self, url, timeout=3.0):
        self.url = url
        self.timeout = timeout
        self.failures = 0

    def fetch(self):
        """The proxy's stats dict, or None."""
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
        except Exception:  # noqa: BLE001
            self.failures += 1
            return None
        self.failures = 0
        if isinstance(data, dict) and isinstance(data.get("stats"), dict):
            return data["stats"]
        return data if isinstance(data, dict) else None