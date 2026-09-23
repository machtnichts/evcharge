"""Minimal Modbus/TCP client - **read-only** (FC3/FC4), stdlib only.

The write primitives (FC6/FC16) were removed on purpose: this client serves the
inverter, and the app must never write to it (the battery belongs to the inverter,
and the owner's rule is that nothing here steers it). With no write method on the
class, "this app cannot write to the inverter" is a property a reader can check in
one glance - and `tests/test_solaredge_decode.py` checks it for them.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import List, Optional


class ModbusClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 1503, unit: int = 1,
                 timeout: float = 6.0, retries: int = 1):
        # retries=1 on purpose: this client normally talks to the local proxy, which
        # already reconnects on its own. Retrying here multiplies the requests a weak
        # inverter has to serve - and the app schedules its own backoff between reads.
        self.host, self.port, self.unit = host, port, unit
        self.timeout, self.retries = timeout, retries
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._tid = 0
        self.stats = {"reads": 0, "errors": 0, "reconnects": 0}

    def _connect(self) -> None:
        if self._sock is not None:
            return
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        self.stats["reconnects"] += 1

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _txn(self, pdu: bytes, expect: bool = True) -> bytes:
        self._connect()
        self._tid = (self._tid + 1) & 0xFFFF or 1
        tid = self._tid
        assert self._sock is not None
        self._sock.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, self.unit) + pdu)
        if not expect:
            return b""
        hdr = b""
        while len(hdr) < 7:
            chunk = self._sock.recv(7 - len(hdr))
            if not chunk:
                raise IOError("connection closed")
            hdr += chunk
        rtid, _pid, length, _uid = struct.unpack(">HHHB", hdr)
        if rtid != tid:
            raise IOError("transaction id mismatch")
        body = b""
        while len(body) < length - 1:
            chunk = self._sock.recv(length - 1 - len(body))
            if not chunk:
                break
            body += chunk
        if not body:
            raise IOError("empty response")
        if body[0] & 0x80:
            raise IOError("modbus exception 0x%02x" % body[1])
        return body

    def _retry(self, fn):
        last: Optional[Exception] = None
        for _ in range(self.retries):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last = exc
                self.stats["errors"] += 1
                self.close()
                time.sleep(0.25)
        raise IOError("%s" % last)

    def read(self, address: int, count: int, fc: int = 3) -> List[int]:
        def once() -> List[int]:
            with self._lock:
                body = self._txn(struct.pack(">BHH", fc, address, count))
                if body[0] != fc or body[1] != count * 2:
                    raise IOError("bad response framing")
                self.stats["reads"] += 1
                return list(struct.unpack(">%dH" % count, body[2:2 + body[1]]))
        return self._retry(once)
