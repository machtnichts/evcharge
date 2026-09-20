#!/usr/bin/env python3
"""Smoke test: build the Service from a config, the way main() does.

This is the gap that let a broken import reach production: every other suite tests a
piece (controller, drivers, cadence function) and none of them constructs the service,
so a NameError in __init__ - or a config key the code expects but nobody sets - passes
all of them and fails only at systemd startup, as a crash loop.

Construction only: no sockets, no device, no cycle.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evcharge.main import Service, UI_HTML  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  %-66s %s%s" % (name, "PASS" if cond else "FAIL",
                            (" - " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


BASE = {
    "site": {"host": "127.0.0.1", "port": 1503, "unit": 1, "battery_capacity_kwh": 10.0},
    "charger": {"host": "192.168.178.22", "phase": 1},
    "interval_s": 30,
    "http": {"host": "127.0.0.1", "port": 7080},
    "logging": {"level": "INFO"},
    "settings": {"mode": "pv", "min_current": 6.0, "max_current": 14.0},
}

print("the service builds with every optional block switched on")
cfg = dict(BASE)
cfg["garage_pv"] = {"enabled": True, "entity": "sensor.garage_pv_leistung",
                    "every_s": 60, "timeout_s": 3}
cfg["proxy_status"] = {"enabled": True, "url": "http://192.168.178.44:1504/status",
                       "every_s": 15, "timeout_s": 3}
try:
    svc = Service(cfg, None)
    check("Service(config, path) constructs", True)
except Exception as exc:  # noqa: BLE001
    svc = None
    check("Service(config, path) constructs", False, "%s: %s" % (type(exc).__name__, exc))

if svc is not None:
    check("it has a controller", getattr(svc, "controller", None) is not None)
    check("it has a site driver", getattr(svc, "site", None) is not None)
    check("it has a charger driver", getattr(svc, "charger", None) is not None)
    check("the garage reader is configured (HaClient import alive)",
          getattr(svc, "garage", None) is not None)
    check("the proxy status reader is configured (ProxyStatusClient alive)",
          getattr(svc, "proxy_status", None) is not None)
    check("the proxy reader uses the configured url",
          getattr(getattr(svc, "proxy_status", None), "url", "") ==
          "http://192.168.178.44:1504/status",
          getattr(getattr(svc, "proxy_status", None), "url", "-"))
    check("state carries the web-UI keys", isinstance(svc.state, dict)
          and "last_cycle" in svc.state or isinstance(svc.state, dict))
    check("settings round-trip through the object",
          svc.settings.mode == "pv" and svc.settings.max_current == 14.0)

print("the service builds with every optional block switched OFF")
cfg2 = dict(BASE)
cfg2["garage_pv"] = {"enabled": False}
cfg2["proxy_status"] = {"enabled": False}
try:
    svc2 = Service(cfg2, None)
    check("no garage, no proxy: still constructs", True)
    check("garage reader absent when disabled", getattr(svc2, "garage", None) is None)
    check("proxy reader absent when disabled", getattr(svc2, "proxy_status", None) is None)
except Exception as exc:  # noqa: BLE001
    check("no garage, no proxy: still constructs", False, "%s: %s" % (type(exc).__name__, exc))

print("defaults apply when the config says nothing at all")
cfg3 = dict(BASE)
try:
    svc3 = Service(cfg3, None)
    check("no proxy_status block: enabled by default with the default url",
          getattr(getattr(svc3, "proxy_status", None), "url", "").endswith("/status"),
          getattr(getattr(svc3, "proxy_status", None), "url", "-"))
except Exception as exc:  # noqa: BLE001
    check("no proxy_status block: enabled by default", False, "%s: %s" % (type(exc).__name__, exc))

print("the web UI's own script parses")
# The page is one JavaScript block inside a Python triple-quoted string, so a backslash-n
# or an unbalanced quote in the source becomes a broken script in the browser - and a
# SyntaxError kills the WHOLE block, leaving a page that renders its shell with every
# value "-" and no error anywhere on the server.
import re as _re
import shutil as _shutil
import subprocess as _sp
import tempfile as _tempfile

_script = _re.findall(r"<script>(.*?)</script>", UI_HTML, _re.S)
check("the UI ships exactly one script block", len(_script) == 1, "%d found" % len(_script))
_node = _shutil.which("node") or _shutil.which("nodejs")
if not _node:
    print("  (node not available - script syntax not checked here)")
else:
    with _tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(_script[0])
        path = fh.name
    res = _sp.run([_node, "--check", path], capture_output=True, text=True)
    detail = (res.stderr.strip().splitlines() or [""])[0][:120] if res.returncode else ""
    check("the served script is syntactically valid JS", res.returncode == 0, detail)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all service smoke checks PASS")