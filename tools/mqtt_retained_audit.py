#!/usr/bin/env python3
"""What does this service still have lying around in the MQTT broker?

Discovery messages are published RETAINED, so an entity that a newer version no longer
publishes keeps existing in Home Assistant for ever - the app has forgotten it, the broker
has not. This lists the retained discovery topics under this service's prefix and can delete
one (an empty retained payload is how MQTT removes a retained topic), which is how a removed
entity is taken out of Home Assistant properly.

Read-only by default; it only writes when told which topic to clear:
    python3 tools/mqtt_retained_audit.py                       # list
    python3 tools/mqtt_retained_audit.py --clear <topic>       # delete one retained topic

Credentials come from config.json (mqtt block) and are never printed.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from evcharge.mqtt import MqttClient  # noqa: E402

CONFIG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
MQ = CONFIG.get("mqtt") or {}
if not MQ.get("enabled", True):
    print("mqtt is disabled in config.json - nothing to look at")
    sys.exit(0)

prefix = MQ.get("discovery_prefix", "homeassistant")
pattern = "%s/+/%s/+/config" % (prefix, MQ.get("node_id", "evcharge_wt"))

args = sys.argv[1:]
clear = None
if len(args) == 2 and args[0] == "--clear":
    clear = args[1]

client = MqttClient(MQ.get("host", "127.0.0.1"), int(MQ.get("port", 1883)),
                    client_id="evcharge-retained-audit",
                    user=MQ.get("user", ""), password=MQ.get("password", ""), keepalive=30)
client.connect()

if clear is not None:
    if not clear.endswith("/config") or "evcharge" not in clear:
        print("refusing to clear %r - not a discovery topic of this service" % clear)
        sys.exit(2)
    client.publish(clear, b"", retain=True)
    time.sleep(1.0)
    print("cleared (empty retained payload sent to): %s" % clear)
    sys.exit(0)

client.subscribe(pattern)
print("subscribed to %s - waiting 6 s for retained messages" % pattern)
seen = {}
deadline = time.time() + 6
while time.time() < deadline:
    pkt = client.read_packet(timeout=1.0)
    if pkt and pkt[0] == "publish":
        _, topic, payload = pkt
        seen[topic] = payload

print("\nretained discovery topics under %s: %d" % (prefix, len(seen)))
for topic in sorted(seen):
    kind, node, oid = topic.split("/")[1:4]
    body = seen[topic]
    body = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else body
    print("  %-54s %s" % (topic, "EMPTY (deletion marker)" if not body else "set"))
print("\nany topic mentioning 'plan': %s"
      % (", ".join(t for t in seen if "plan" in t.lower()) or "none"))
