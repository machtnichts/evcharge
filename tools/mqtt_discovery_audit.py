#!/usr/bin/env python3
"""Audit the app's retained MQTT discovery messages and clear orphaned ones.

Home Assistant creates an entity from every retained `.../config` message. If an older version
leaves its message in the broker, its entity stays in HA for ever - which is exactly what
happened with `binary_sensor.ev_charger_wt_ev_charge_control_enabled` (an older version
published a binary_sensor there instead of a switch).

Without an argument: report only. With `--clean`: delete everything that does not belong to
the current set (empty payload, retain=True).
"""
import json
import sys

sys.path.insert(0, "/home/adermake/EV-CHARGER-WT-HA/ha-app")
from evcharge.mqtt import MqttClient  # noqa: E402

# What the current version publishes (mqtt.py, publish_discovery):
CURRENT = {("sensor", o) for o in ("pv_power", "grid_power", "battery_power", "battery_soc",
                                   "ev_power", "ev_current", "ev_surplus", "grid_import",
                                   "grid_export", "session_energy")}
CURRENT |= {("binary_sensor", "charging"), ("select", "mode"), ("switch", "control_enabled")}
CURRENT |= {("number", o) for o in ("max_current", "min_current", "buffer_soc", "priority_soc")}

CFG = json.load(open("/home/adermake/EV-CHARGER-WT-HA/ha-app/config.json"))["mqtt"]
PATTERN = CFG["discovery_prefix"] + "/+/evcharge_wt/+/config"
c = MqttClient(host=CFG["host"], port=int(CFG["port"]), client_id=CFG["client_id"] + "-disc",
               user=CFG["user"], password=CFG["password"])
c.connect()
c.subscribe([PATTERN])
found = {}
for _ in range(60):
    pkt = c.read_packet(timeout=0.5)
    if pkt and pkt[0] == "publish":
        found[pkt[1]] = pkt[2] if isinstance(pkt[2], str) else pkt[2].decode("utf-8", "replace")
print("retained discovery messages under %s: %d" % (PATTERN, len(found)))
orphans = []
for topic in sorted(found):
    parts = topic.split("/")           # homeassistant / <kind> / evcharge_wt / <oid> / config
    kind, oid = parts[1], parts[3]
    known = (kind, oid) in CURRENT
    if not known:
        orphans.append(topic)
    print("  %-58s %s" % ("%s/%s" % (kind, oid), "current" if known else "ORPHANED"))
print()
if not orphans:
    print("nothing to clean up")
    c.close()
    sys.exit(0)
if "--clean" not in sys.argv:
    print("run again with --clean to delete:")
    for t in orphans:
        print("  %s" % t)
    c.close()
    sys.exit(0)
for t in orphans:
    c.publish(t, "", retain=True)
    print("  deleted (empty, retained): %s" % t)
c.close()
