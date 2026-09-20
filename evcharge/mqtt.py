"""Minimal MQTT 3.1.1 client (stdlib only) + Home Assistant MQTT discovery.

Only what this service needs: CONNECT (with optional auth), PUBLISH at QoS 0,
PINGREQ keepalive, DISCONNECT. Runs in its own thread with a small outbound
queue, so a broker outage never blocks or crashes the control loop.

Entities published (all under the configured discovery prefix):
    sensor  pv_power, grid_power, battery_power, battery_soc, ev_power,
            ev_energy_grid_import, ev_energy_grid_export, ev_current, ev_surplus
    select  mode            (off / now / minpv / pv)
    number  max_current, min_current, buffer_soc, priority_soc, plan_energy_kwh
    binary_sensor  charging
    switch  control_enabled
"""
from __future__ import annotations

import json
import logging
import queue
import socket
import struct
import threading
import time
from typing import Dict, Optional

LOG = logging.getLogger("evcharge.mqtt")

DEVICE = {
    "identifiers": ["evcharge_wt"],
    "name": "EV Charger WT",
    "manufacturer": "local",
    "model": "SolarEdge + go-e HOME+",
}


def _topic(base: str, *parts: str) -> str:
    return "/".join([base.rstrip("/")] + [p.strip("/") for p in parts if p])


class MqttClient:
    def __init__(self, host: str, port: int = 1883, client_id: str = "evcharge",
                 user: str = "", password: str = "", keepalive: int = 60,
                 timeout: float = 5.0):
        self.host, self.port = host, int(port)
        self.client_id = client_id
        self.user, self.password = user, password
        self.keepalive = keepalive
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._last_out = 0.0
        self._pid = 0

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        flags = 0x02                                   # clean session
        payload = self._str(self.client_id)
        if self.user:
            flags |= 0x80
            payload += self._str(self.user)
            if self.password:
                flags |= 0x40
                payload += self._str(self.password)
        var = self._str("MQTT") + bytes([4, flags]) + struct.pack(">H", self.keepalive)
        self._sock.sendall(bytes([0x10]) + self._remaining(len(var) + len(payload)) + var + payload)
        resp = self._sock.recv(4)
        if len(resp) < 4 or resp[0] != 0x20:
            raise IOError("unexpected CONNACK: %r" % resp)
        if resp[3] != 0:
            raise IOError("broker refused connection, code %d" % resp[3])
        self._last_out = time.time()
        LOG.info("MQTT connected to %s:%d", self.host, self.port)

    @staticmethod
    def _str(s: str) -> bytes:
        b = s.encode("utf-8")
        return struct.pack(">H", len(b)) + b

    @staticmethod
    def _remaining(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n % 128
            n //= 128
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    def publish(self, topic: str, payload, retain: bool = True) -> None:
        if self._sock is None:
            self.connect()
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        body = self._str(topic) + (payload.encode("utf-8") if isinstance(payload, str) else payload)
        header = bytes([0x30 | (0x01 if retain else 0)])
        self._sock.sendall(header + self._remaining(len(body)) + body)
        self._last_out = time.time()

    def subscribe(self, topics) -> None:
        """Subscribe at QoS 0 to one topic or a list of topics."""
        if self._sock is None:
            self.connect()
        if isinstance(topics, str):
            topics = [topics]
        body = struct.pack(">H", self._next_pid())
        for t in topics:
            body += self._str(t) + b"\x00"
        self._sock.sendall(b"\x82" + self._remaining(len(body)) + body)
        self._last_out = time.time()

    def _next_pid(self) -> int:
        self._pid = (self._pid % 65535) + 1
        return self._pid

    def read_packet(self, timeout: float = 1.0) -> Optional[tuple]:
        """Read one inbound packet. Returns ('publish', topic, payload) or
        ('other', type, None), or None on timeout."""
        if self._sock is None:
            return None
        old = self._sock.gettimeout()
        try:
            self._sock.settimeout(timeout)
            first = self._sock.recv(1)
            if not first:
                raise IOError("broker closed connection")
            ptype = first[0] >> 4
            multiplier, length, shift = 1, 0, 0
            while True:
                b = self._sock.recv(1)
                if not b:
                    raise IOError("truncated length")
                length += (b[0] & 0x7F) * multiplier
                multiplier *= 128
                shift += 1
                if not (b[0] & 0x80) or shift > 3:
                    break
            body = b""
            while len(body) < length:
                chunk = self._sock.recv(length - len(body))
                if not chunk:
                    break
                body += chunk
            if ptype == 3 and len(body) >= 2:                      # PUBLISH
                tlen = struct.unpack(">H", body[:2])[0]
                topic = body[2:2 + tlen].decode("utf-8", "replace")
                payload = body[2 + tlen:].decode("utf-8", "replace")
                return ("publish", topic, payload)
            return ("other", ptype, None)
        except socket.timeout:
            return None
        finally:
            try:
                if self._sock is not None:
                    self._sock.settimeout(old)
            except OSError:
                pass

    def keepalive_tick(self) -> None:
        if self._sock is not None and time.time() - self._last_out > self.keepalive * 0.7:
            self._sock.sendall(b"\xc0\x00")
            self._last_out = time.time()

    def close(self) -> None:
        try:
            if self._sock:
                self._sock.sendall(b"\xe0\x00")
                self._sock.close()
        except OSError:
            pass
        self._sock = None


class HomeAssistantMqtt:
    """Publishes discovery config + live state for Home Assistant."""

    def __init__(self, service, config: Dict):
        self.svc = service
        self.cfg = config
        self.prefix = config.get("topic_prefix", "evcharge")
        self.disc = config.get("discovery_prefix", "homeassistant")
        self.client = MqttClient(config.get("host", "localhost"), config.get("port", 1883),
                                 client_id=config.get("client_id", "evcharge-wt"),
                                 user=config.get("user", ""), password=config.get("password", ""))
        self.q: "queue.Queue[tuple]" = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._threads = []
        self._discovery_sent = False

    # -- discovery -------------------------------------------------------
    def _entity(self, kind: str, oid: str, name: str, extra: Dict) -> None:
        cfg = {
            "name": name,
            "unique_id": "evcharge_%s" % oid,
            "state_topic": _topic(self.prefix, "state"),
            "availability_topic": _topic(self.prefix, "status"),
            "device": DEVICE,
        }
        cfg.update(extra)
        topic = _topic(self.disc, kind, "evcharge_wt", oid, "config")
        self.client.publish(topic, cfg, retain=True)

    def publish_discovery(self) -> None:
        st = "{{ value_json.%s }}"
        for oid, name, key, unit, cls in (
            ("pv_power", "PV power", "site.pv_power_w", "W", "power"),
            ("grid_power", "Grid power", "site.grid_power_w", "W", "power"),
            ("battery_power", "Battery power", "site.battery_power_w", "W", "power"),
            ("battery_soc", "Battery SOC", "site.battery_soc", "%", "battery"),
            ("ev_power", "EV charging power", "charger.power", "W", "power"),
            ("ev_current", "EV current", "decision.target_current", "A", "current"),
            ("ev_surplus", "PV surplus for EV", "decision.surplus_w", "W", "power"),
            ("grid_import", "Grid import total", "site.grid_import_kwh", "kWh", "energy"),
            ("grid_export", "Grid export total", "site.grid_export_kwh", "kWh", "energy"),
            ("session_energy", "Charging session energy", "session_kwh", "kWh", "energy"),
        ):
            self._entity("sensor", oid, name, {
                "state_topic": _topic(self.prefix, "state"),
                "value_template": st % key,
                "unit_of_measurement": unit,
                "device_class": cls,
                "state_class": "measurement" if cls != "energy" else "total_increasing",
                "json_attributes_topic": _topic(self.prefix, "state"),
            })
        self._entity("binary_sensor", "charging", "EV charging", {
            "value_template": "{{ value_json.charger.charging }}",
            "device_class": "plug",
        })
        self._entity("select", "mode", "Charging mode", {
            "value_template": "{{ value_json.mode }}",
            "command_topic": _topic(self.prefix, "set/mode"),
            "options": ["off", "now", "minpv", "pv"],
        })
        self._entity("switch", "control_enabled", "EV charge control enabled", {
            "value_template": "{{ value_json.control_enabled }}",
            "command_topic": _topic(self.prefix, "set/control_enabled"),
            "payload_on": "true", "payload_off": "false",
        })
        for oid, name, unit in (("max_current", "EV max current", "A"),
                                ("min_current", "EV min current", "A"),
                                ("buffer_soc", "Battery buffer SOC", "%"),
                                ("priority_soc", "Battery priority SOC", "%"),
                                ("plan_energy_kwh", "Charge plan energy", "kWh")):
            self._entity("number", oid, name, {
                "value_template": "{{ value_json.settings.%s }}" % oid,
                "command_topic": _topic(self.prefix, "set", oid),
                "unit_of_measurement": unit,
                "min": 0, "max": 32 if unit == "A" else (100 if unit == "%" else 100),
                "step": 0.5 if unit == "A" else 1,
                "mode": "box",
            })
        LOG.info("Home Assistant discovery published")
        self._discovery_sent = True

    # -- commands --------------------------------------------------------
    NUMERIC = ("min_current", "max_current", "buffer_soc", "priority_soc",
               "plan_energy_kwh", "enable_threshold_w", "residual_power_w")

    def handle_command(self, topic: str, payload: str) -> None:
        prefix = _topic(self.prefix, "set") + "/"
        if not topic.startswith(prefix):
            return
        key = topic[len(prefix):]
        payload = (payload or "").strip()
        try:
            if key == "mode":
                if payload not in ("off", "now", "minpv", "pv"):
                    LOG.warning("ignoring unknown mode %r", payload)
                    return
                self.svc.update_settings({"mode": payload})
            elif key == "control_enabled":
                self.svc.update_settings(
                    {"control_enabled": payload.lower() in ("true", "1", "on", "yes")})
            elif key in self.NUMERIC:
                self.svc.update_settings({key: float(payload)})
            elif key == "cheap_hours":
                self.svc.update_settings({"cheap_hours": payload})
            elif key == "plan_deadline":
                self.svc.update_settings({"plan_deadline": payload})
            else:
                LOG.warning("unknown command topic %s", topic)
        except (TypeError, ValueError) as exc:
            LOG.warning("bad command payload for %s: %s", key, exc)

    def _ensure_connected(self) -> None:
        if self.client._sock is None:
            self.client.connect()
            self.client.subscribe(_topic(self.prefix, "set/#"))
            LOG.info("subscribed to %s", _topic(self.prefix, "set/#"))

    # -- state -----------------------------------------------------------
    def publish_state(self, state: Dict) -> None:
        self.q.put_nowait(("state", state))

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                kind, payload = self.q.get(timeout=0.5)
            except queue.Empty:
                kind, payload = None, None
            try:
                self._ensure_connected()
                if not self._discovery_sent:
                    self.publish_discovery()
                    self.client.publish(_topic(self.prefix, "status"), "online", retain=True)
                if kind == "state":
                    self.client.publish(_topic(self.prefix, "state"), payload, retain=True)
                else:
                    self.client.keepalive_tick()
                pkt = self.client.read_packet(timeout=0.1)
                while pkt is not None:
                    if pkt[0] == "publish":
                        self.handle_command(pkt[1], pkt[2])
                    pkt = self.client.read_packet(timeout=0.01)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("mqtt cycle error: %s", exc)
                self.client.close()
                time.sleep(2)

    def start(self) -> None:
        self.client.connect()
        self.client.subscribe(_topic(self.prefix, "set/#"))
        t = threading.Thread(target=self._worker, name="mqtt", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self.client.close()
