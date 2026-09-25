#!/usr/bin/env python3
"""
Loopback MQTT test: a throwaway TCP server that speaks just enough MQTT 3.1.1 to
verify this client end-to-end (no real broker needed).

Checks: CONNECT/CONNACK, SUBSCRIBE to the command topic, discovery configs are
published, state lands on the state topic, and an inbound command on
set/mode actually reaches the service.
"""
import json
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evcharge.mqtt import MqttClient, HomeAssistantMqtt  # noqa: E402

received = []
subscribed = []
conn_ok = threading.Event()


def read_packet(conn):
    first = conn.recv(1)
    if not first:
        return None
    ptype = first[0] >> 4
    multiplier, length = 1, 0
    while True:
        b = conn.recv(1)
        if not b:
            return None
        length += (b[0] & 0x7F) * multiplier
        multiplier *= 128
        if not (b[0] & 0x80):
            break
    body = b""
    while len(body) < length:
        chunk = conn.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    return ptype, body


def send_publish(conn, topic, payload):
    tb = topic.encode()
    body = struct.pack(">H", len(tb)) + tb + payload.encode()
    n = len(body)
    rem = b""
    while True:
        d = n % 128
        n //= 128
        rem += bytes([d | 0x80]) if n else bytes([d])
        if not n:
            break
    conn.sendall(bytes([0x30]) + rem + body)


def fake_broker(srv):
    conn, _ = srv.accept()
    with conn:
        pkt = read_packet(conn)
        assert pkt and pkt[0] == 1, "expected CONNECT, got %r" % (pkt,)
        conn.sendall(b"\x20\x02\x00\x00")           # CONNACK accepted
        conn_ok.set()
        deadline = time.time() + 12
        while time.time() < deadline:
            conn.settimeout(1.0)
            try:
                pkt = read_packet(conn)
            except socket.timeout:
                continue
            if pkt is None:
                break
            ptype, body = pkt
            if ptype == 8:                          # SUBSCRIBE
                pid = struct.unpack(">H", body[:2])[0]
                tlen = struct.unpack(">H", body[2:4])[0]
                subscribed.append(body[4:4 + tlen].decode())
                conn.sendall(b"\x90\x03" + struct.pack(">H", pid) + b"\x00")
            elif ptype == 3:                        # PUBLISH
                tlen = struct.unpack(">H", body[:2])[0]
                received.append((body[2:2 + tlen].decode(), body[2 + tlen:].decode()))
            elif ptype == 12:                       # PINGREQ
                conn.sendall(b"\xd0\x00")
        # push a command back to the service
        send_publish(conn, "evcharge/set/mode", "now")
        time.sleep(1.0)
        send_publish(conn, "evcharge/set/max_current", "11")
        time.sleep(1.0)


class StubService:
    def __init__(self):
        self.calls = []

    def update_settings(self, patch):
        self.calls.append(patch)
        return {"ok": True}


srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]
th = threading.Thread(target=fake_broker, args=(srv,), daemon=True)
th.start()

svc = StubService()
ha = HomeAssistantMqtt(svc, {"host": "127.0.0.1", "port": port, "topic_prefix": "evcharge",
                             "discovery_prefix": "homeassistant"})
ha.start()
if not conn_ok.wait(5):
    print("FAIL: no connection to fake broker")
    sys.exit(1)
print("  broker accepted connection          PASS")

ha.publish_state({"mode": "pv", "control_enabled": False,
                  "site": {"pv_power_w": 2500, "battery_soc": 84.9},
                  "charger": {"charging": True, "power": 1350},
                  "settings": {"max_current": 14, "min_current": 6}})
time.sleep(2.0)
th.join(timeout=15)

topics = [t for t, _ in received]
print("  discovered entities published       %s (%d configs)"
      % ("PASS" if any(t.startswith("homeassistant/") for t in topics) else "FAIL",
         sum(1 for t in topics if t.startswith("homeassistant/"))))
print("  state published                     %s"
      % ("PASS" if "evcharge/state" in topics else "FAIL"))
if "evcharge/state" in topics:
    payload = json.loads([p for t, p in received if t == "evcharge/state"][-1])
    print("      pv_power_w=%s battery_soc=%s" % (payload["site"]["pv_power_w"],
                                                  payload["site"]["battery_soc"]))
print("  subscribed to commands              %s (%s)"
      % ("PASS" if any(s.endswith("set/#") for s in subscribed) else "FAIL", subscribed))

mode_calls = [c for c in svc.calls if c.get("mode") == "now"]
amp_calls = [c for c in svc.calls if "max_current" in c]
print("  inbound set/mode applied            %s (%s)"
      % ("PASS" if mode_calls else "FAIL", svc.calls))
print("  inbound set/max_current applied     %s" % ("PASS" if amp_calls else "FAIL"))

expected = ("homeassistant/sensor/evcharge_wt/pv_power/config" in topics and
            "evcharge/state" in topics and mode_calls and amp_calls and
            any(s.endswith("set/#") for s in subscribed))

# Jinja renders a JSON boolean as "True"/"False" - neither the binary sensor (ON/OFF) nor the
# switch (payload_off "false") matches that, and both entities stayed unknown until the
# templates spelled it out. Pinned on the source because Home Assistant renders them, not this
# client: a silent regression here means two dead entities in HA, not a failed test.
_mqtt_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "evcharge", "mqtt.py")).read()
_bool_ok = ("{{ 'ON' if value_json.charger.charging else 'OFF' }}" in _mqtt_src
            and "{{ 'true' if value_json.control_enabled else 'false' }}" in _mqtt_src)
print("  boolean templates spelled out        %s" % ("PASS" if _bool_ok else "FAIL"))
expected = expected and _bool_ok
print("\nRESULT: %s" % ("ALL PASS" if expected else "FAILURES PRESENT"))
sys.exit(0 if expected else 1)
