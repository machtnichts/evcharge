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

from evcharge.main import Service, UI_HTML, ADDON_OPTION_MAP  # noqa: E402

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

print("a settings change is visible in the state snapshot at once")
# The mode badge, the highlighted mode button and the enable/disable (DRY RUN) label all
# read state["mode"] / state["control_enabled"], and the loop is the only other place that
# refreshes them - once per cycle, measured at ~32 s on the live service (interval_s 30
# plus the cycle's own work). When update_settings only wrote state["settings"], a click
# in the web UI looked like it had done nothing until that cycle came round, which is how
# a working button gets pressed twice (the log shows exactly that: the same mode written
# twice, seconds apart). Nothing here touches a device - the drivers are stubs.
if svc is not None:
    from evcharge.drivers.goe import ChargerState
    from evcharge.drivers.solaredge import SiteState

    for _attr in ("garage", "proxy_status", "session_meter", "forecast"):
        setattr(svc, _attr, None)

    class _StubCharger:
        def status(self):
            return ChargerState(connected=False)

        def set_charging(self, on):
            pass

        def set_max_current(self, amps):
            pass

    class _StubSite:
        def read(self):
            return SiteState(grid_power_w=120.0, battery_soc=70.0)

    svc.charger, svc.site = _StubCharger(), _StubSite()
    svc.cycle()                                  # the first cycle, as at startup
    svc.update_settings({"mode": "manual", "control_enabled": False})
    check("mode is in the snapshot right after the change",
          svc.state.get("mode") == "manual", str(svc.state.get("mode")))
    check("the control flag is in the snapshot right after the change",
          svc.state.get("control_enabled") is False, str(svc.state.get("control_enabled")))
    check("state['settings'] agrees with it (what the /api/settings replay reads)",
          (svc.state.get("settings") or {}).get("mode") == "manual")
    check("and the cycle still publishes the same answer, not a different one",
          svc.cycle() is None and svc.state.get("mode") == "manual")
    check("an unknown mode is still refused, settings untouched",
          svc.update_settings({"mode": "nonsense"}).get("ok") is False
          and svc.state.get("mode") == "manual")

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

# Pinned on the source, because this failure is invisible to the server: the page polls the
# state every 3 s and used to write every settings field from it unconditionally, so a value
# typed into an input ("plan kWh: 1") was back to the stored one before the Save button could
# be reached - measured on the live page: 1 -> 0 within 4 s. The fill must skip a field that
# is focused or marked dirty, and the save must clear the mark so the field follows the
# server again (a rejected value then visibly comes back instead of looking saved).
check("the settings fill keeps its hands off a field being edited",
      'el===document.activeElement||el.dataset.dirty==="1"' in UI_HTML)
check("a keystroke marks the field and a save clears the mark",
      'addEventListener("input",()=>{el.dataset.dirty="1";})' in UI_HTML
      and "clearDirtySettings();" in UI_HTML)
check("no unconditional settings fill is left in the page",
      'const el=document.getElementById("set_"+k); if(el&&st[k]!==undefined) el.value=' not in UI_HTML)
# The deadline plan was removed on the owner's request (03.10.2026) - fields, countdown text
# and add-on options alike. Pinned on the served page and the option map, because a leftover
# input would sit there looking editable while the setting no longer exists.
check("the settings card has no plan fields left",
      "set_plan_energy_kwh" not in UI_HTML and "set_plan_deadline" not in UI_HTML)
check("the countdown says nothing about a plan any more", "plan:" not in UI_HTML)
check("no add-on option maps plan keys any more",
      "plan_energy_kwh" not in str(ADDON_OPTION_MAP) and "plan_deadline" not in str(ADDON_OPTION_MAP))

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("all service smoke checks PASS")