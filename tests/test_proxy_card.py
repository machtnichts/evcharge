#!/usr/bin/env python3
"""Tests for the proxy-health card: classification, field extraction, robustness.

The headline is what the owner reads to answer "is something wrong?", so each level gets
a test, including the two cases that matter most: a frozen-reading proxy that still
answers (stale) and a proxy that has written to the device (must never happen).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.proxy import (  # noqa: E402
    classify, summary, ProxyStatusClient, AGE_WARN_S, AGE_BAD_S,
)

FAILS = []
NOW = 1_800_000_000.0


def check(name, cond, detail=""):
    print("  %-66s %s%s" % (name, "PASS" if cond else "FAIL",
                            (" - " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


def stats(**kw):
    base = {"last_upstream_ok": NOW - 5, "upstream_backoff_s": 0, "upstream_errors": 0,
            "upstream_reconnects": 0, "upstream_timeouts": 0, "upstream_reads": 40,
            "upstream_writes": 0, "requests_total": 60, "requests_cache_hit": 4,
            "requests_cache_miss": 56, "uptime_s": 3600, "clients_active": 1,
            "last_upstream_error": "", "validation_failures": 0}
    base.update(kw)
    return base


print("a healthy link reads as OK")
c = classify(stats(), NOW)
check("fresh read -> level ok, headline PROXY OK", c["level"] == "ok" and c["text"] == "PROXY OK", c["text"])
check("the age of the last good read is reported", abs(c["ok_age_s"] - 5) < 0.01, str(c["ok_age_s"]))

print("an aging reading warns before it is wrong")
c = classify(stats(last_upstream_ok=NOW - AGE_WARN_S - 1), NOW)
check("older than the warn threshold -> level warn", c["level"] == "warn", c["text"])
check("headline says how old", "OLD" in c["text"], c["text"])

print("a frozen reading behind a working proxy is the dangerous case")
c = classify(stats(last_upstream_ok=NOW - AGE_BAD_S - 1), NOW)
check("older than the bad threshold -> level bad", c["level"] == "bad", c["text"])
check("headline says stale, in minutes", c["text"].startswith("PROXY STALE"), c["text"])
check("it is NOT reported as unreachable (the proxy still answers)",
      "UNREACHABLE" not in c["text"], c["text"])

print("a backing-off proxy is visibly not normal")
c = classify(stats(upstream_backoff_s=60), NOW)
check("backoff > 0 -> level warn", c["level"] == "warn", c["text"])
check("headline names the backoff", "BACKING OFF 60" in c["text"], c["text"])

print("writes are the one thing that must never happen")
c = classify(stats(upstream_writes=1), NOW)
check("a write to the device -> level bad", c["level"] == "bad", c["text"])
check("headline says so plainly", "WROTE" in c["text"], c["text"])
check("writes outrank a healthy reading", c["text"].startswith("PROXY WROTE"), c["text"])

print("no proxy at all, and no good read yet")
check("no stats -> UNREACHABLE", classify(None, NOW)["text"] == "PROXY UNREACHABLE")
check("no stats -> level bad", classify(None, NOW)["level"] == "bad")
check("proxy up but never read -> warns, not fake-ok",
      classify(stats(last_upstream_ok=0), NOW)["level"] == "warn",
      classify(stats(last_upstream_ok=0), NOW)["text"])

print("robustness: a wrong or hostile answer must not break the card")
check("garbage values do not raise", classify({"last_upstream_ok": "abc", "upstream_backoff_s": "??"}, NOW)["level"] in ("warn", "bad"))
check("wrong types do not raise", classify({"last_upstream_ok": [], "upstream_writes": {}}, NOW)["level"] in ("warn", "bad"))
check("empty dict -> warn not crash", classify({}, NOW)["level"] in ("warn", "bad"))

print("the summary carries every field the card renders")
s = summary(stats(), NOW, failures=0)
for key in ("level", "text", "ok_age_s", "backoff_s", "errors", "reconnects", "timeouts",
            "reads", "writes", "requests", "hits", "misses", "uptime_s", "last_error"):
    check("summary has %s" % key, key in s)
check("summary counts add up", s["reads"] == 40 and s["misses"] == 56, str((s["reads"], s["misses"])))
check("last error is carried verbatim", summary(stats(last_upstream_error="read: boom"), NOW)["last_error"] == "read: boom")

print("the age of the last error, for \"vor 15 min\" in the UI")
s_age = summary(stats(last_upstream_error="failed to fill whole buffer",
                      last_upstream_error_at=NOW - 900), NOW)
check("900 s old reads as 900", s_age["last_error_age_s"] == 900.0, s_age["last_error_age_s"])
check("its exact epoch is carried too (for the tooltip)",
      abs(s_age["last_error_at"] - (NOW - 900)) < 0.01, s_age["last_error_at"])
check("no error -> no age at all", summary(stats(), NOW)["last_error_age_s"] is None,
      summary(stats(), NOW)["last_error_age_s"])
check("an empty last-error string is still shown as absent",
      summary(stats(), NOW)["last_error"] == "", summary(stats(), NOW)["last_error"])
check("a nonsense timestamp does not blow up",
      summary(stats(last_upstream_error="x", last_upstream_error_at="abc"), NOW)["last_error_age_s"] is None)

print("the production call shape: summary() without an explicit clock")
# main.py calls proxy_summary(stats, failures=...) - with no `now`. Every check above
# passes a clock, which is why a float(None) crash *per cycle* went unnoticed in a live
# run: the faulty line only executes once the proxy reports an error timestamp, so the
# app was fine for hours and then crash-looped the moment an upstream error appeared.
no_now = summary(stats(last_upstream_error="failed to fill whole buffer",
                       last_upstream_error_at=NOW - 600), failures=0)
check("summary() without now does not raise", isinstance(no_now, dict))
check("its error age is a real number", isinstance(no_now["last_error_age_s"], float),
      no_now["last_error_age_s"])
check("and the age is never negative", no_now["last_error_age_s"] >= 0.0, no_now["last_error_age_s"])
check("explicit clock and no clock agree on the field",
      "last_error_age_s" in summary(stats(), NOW) and "last_error_age_s" in no_now)
check("no error + no clock -> still None, not a crash",
      summary(stats(), failures=2)["last_error_age_s"] is None)
check("classify() keeps working without a clock too", classify(stats())["level"] == "ok")

print("the live proxy (soft check - skipped if it is not running)")
client = ProxyStatusClient("http://127.0.0.1:1504/status", timeout=2.0)
live = client.fetch()
if live is None:
    print("  proxy not reachable right now: live check skipped (failures=%d)" % client.failures)
else:
    check("live status has the counters the card uses",
          "upstream_writes" in live and "upstream_backoff_s" in live, sorted(live)[:4])
    check("live headline is ok or warn", classify(live)["level"] in ("ok", "warn"),
          classify(live)["text"])
    check("live proxy has never written to the device", int(live.get("upstream_writes") or 0) == 0,
          str(live.get("upstream_writes")))
    live_card = summary(live, failures=client.failures)
    check("the live proxy survives the production call shape (no clock)",
          isinstance(live_card, dict) and live_card.get("text"), live_card.get("text"))
    check("a live error timestamp yields an age, not an exception",
          live_card.get("last_error_age_s") is None
          or live_card["last_error_age_s"] >= 0.0, live_card.get("last_error_age_s"))

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all proxy-card checks PASS")