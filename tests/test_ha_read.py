#!/usr/bin/env python3
"""Tests for the display-only Home Assistant reader (evcharge/ha.py).

The point of these checks is not the happy path alone - it is that a failure can only
ever produce "no value", and that the token travels in a header and never in a URL.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evcharge import ha  # noqa: E402

passed = failed = 0
captured = []


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s %s" % (name, detail))


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_urlopen(payload=None, exc=None):
    def _open(req, timeout=None):
        captured.append(req)
        if exc is not None:
            raise exc
        return FakeResp(payload)
    return _open


real_urlopen = ha.urlopen
try:
    print("ha reader: numeric value with age")
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 30))
    ha.urlopen = make_urlopen({"state": "5.6", "last_updated": stamp})
    c = ha.HaClient("sensor.garage_pv_leistung", base_url="http://ha:8123", token="tok")
    got = c.get_state()
    check("value parsed", got and got["value"] == 5.6, repr(got))
    check("age is ~30 s", got and 25 <= (got["age_s"] or 0) <= 40, repr(got))
    check("token is NOT in the URL", "tok" not in captured[-1].full_url, captured[-1].full_url)
    check("token IS in the header",
          captured[-1].headers.get("Authorization") == "Bearer tok")

    print("ha reader: non-numeric states are 'no value', not zero")
    for raw in ("unavailable", "unknown", ""):
        ha.urlopen = make_urlopen({"state": raw})
        got = ha.HaClient("sensor.x", base_url="http://ha:8123", token="tok").get_state()
        check("%r -> value None" % raw, got and got["value"] is None, repr(got))

    print("ha reader: failures return None and are counted, not raised")
    ha.urlopen = make_urlopen(exc=OSError("boom"))
    c = ha.HaClient("sensor.y", base_url="http://ha:8123", token="tok")
    check("first failure -> None", c.get_state() is None)
    check("second failure -> None", c.get_state() is None)
    check("failures counted", c._failures == 2, str(c._failures))

    print("ha reader: a bogus timestamp is tolerated")
    ha.urlopen = make_urlopen({"state": "12", "last_updated": "not-a-date"})
    got = ha.HaClient("sensor.z", base_url="http://ha:8123", token="tok").get_state()
    check("age None, value kept", got and got["value"] == 12.0 and got["age_s"] is None,
          repr(got))

    print("ha reader: credentials from an env file, not from the code")
    env = Path("/tmp/ha_env_test.env")
    env.write_text('# comment\nHASS_URL="http://ha:8123"\nHASS_TOKEN=\'tok-from-file\'\n')
    saved = {k: os.environ.pop(k) for k in ("HASS_URL", "HASS_TOKEN") if k in os.environ}
    try:
        ha.urlopen = make_urlopen({"state": "7"})
        got = ha.HaClient("sensor.garage_pv_leistung", env_file=str(env)).get_state()
        check("value read via env file", got and got["value"] == 7.0, repr(got))
        check("env file filled HASS_URL", os.environ.get("HASS_URL") == "http://ha:8123")
    finally:
        for k in ("HASS_URL", "HASS_TOKEN"):
            os.environ.pop(k, None)
        os.environ.update(saved)

    print("ha reader: missing credentials -> None, no request attempted")
    captured.clear()
    saved = {k: os.environ.pop(k) for k in ("HASS_URL", "HASS_TOKEN") if k in os.environ}
    try:
        c = ha.HaClient("sensor.q", env_file="/tmp/does-not-exist.env")
        check("no credentials -> None", c.get_state() is None)
        check("no request made", captured == [], repr(captured))
    finally:
        os.environ.update(saved)
finally:
    ha.urlopen = real_urlopen

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)