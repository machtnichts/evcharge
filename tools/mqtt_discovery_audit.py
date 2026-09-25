#!/usr/bin/env python3
"""Prueft die retained Discovery-Nachrichten der App und raeumt verwaiste auf.

HA legt aus jeder retained `.../config`-Nachricht eine Entitaet an. Bleibt eine alte Fassung
im Broker liegen, bleibt ihre Entitaet in HA stehen - genau das war bei
`binary_sensor.ev_charger_wt_ev_charge_control_enabled` der Fall (eine aeltere Version hatte
dort einen binary_sensor statt eines switch).

Ohne Argument: nur anzeigen. Mit `--aufraeumen`: alles loeschen, was nicht zum aktuellen Satz
gehoert (leere Payload, retain=True).
"""
import json
import sys

sys.path.insert(0, "/home/adermake/EV-CHARGER-WT-HA/ha-app")
from evcharge.mqtt import MqttClient  # noqa: E402

# Was die aktuelle Fassung anlegt (mqtt.py, publish_discovery):
AKTUELL = {("sensor", o) for o in ("pv_power", "grid_power", "battery_power", "battery_soc",
                                  "ev_power", "ev_current", "ev_surplus", "grid_import",
                                  "grid_export", "session_energy")}
AKTUELL |= {("binary_sensor", "charging"), ("select", "mode"), ("switch", "control_enabled")}
AKTUELL |= {("number", o) for o in ("max_current", "min_current", "buffer_soc", "priority_soc",
                                    "plan_energy_kwh")}

CFG = json.load(open("/home/adermake/EV-CHARGER-WT-HA/ha-app/config.json"))["mqtt"]
PRAEFIX = CFG["discovery_prefix"] + "/+/evcharge_wt/+/config"
c = MqttClient(host=CFG["host"], port=int(CFG["port"]), client_id=CFG["client_id"] + "-disc",
               user=CFG["user"], password=CFG["password"])
c.connect()
c.subscribe([PRAEFIX])
gefunden = {}
for _ in range(60):
    pkt = c.read_packet(timeout=0.5)
    if pkt and pkt[0] == "publish":
        gefunden[pkt[1]] = pkt[2] if isinstance(pkt[2], str) else pkt[2].decode("utf-8", "replace")
print("retained Discovery-Nachrichten unter %s: %d" % (PRAEFIX, len(gefunden)))
verwaist = []
for topic in sorted(gefunden):
    teile = topic.split("/")            # homeassistant / <kind> / evcharge_wt / <oid> / config
    kind, oid = teile[1], teile[3]
    drin = (kind, oid) in AKTUELL
    if not drin:
        verwaist.append(topic)
    print("  %-58s %s" % ("%s/%s" % (kind, oid), "aktuell" if drin else "VERWAIST"))
print()
if not verwaist:
    print("nichts aufzuraeumen")
    c.close()
    sys.exit(0)
if "--aufraeumen" not in sys.argv:
    print("Zum Loeschen erneut mit --aufraeumen aufrufen:")
    for t in verwaist:
        print("  %s" % t)
    c.close()
    sys.exit(0)
for t in verwaist:
    c.publish(t, "", retain=True)
    print("  geloescht (leer, retained): %s" % t)
c.close()
