"""Read single entity states from Home Assistant - display-only values.

Some numbers live neither in the SolarEdge registers nor in the wallbox: the garage
PV array has its own poller and only exists in HA. This module fetches such a value
for the info rows of the web UI.

Two rules shape it:

* **It can never affect control.** Every failure path returns None after a short
  timeout, the last good value stays in the caller's state, and nothing here is
  consulted by the controller. An HA outage costs an info row, not a charge.
* **The token never leaks.** It is read from the process environment or from the
  same 0600 env file the rest of the tooling uses, sent only as an Authorization
  header - never in a URL, never logged, never in an exception message we raise.

The import of urlopen is module-level on purpose so tests can patch `ha.urlopen`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from urllib.request import Request, urlopen

LOG = logging.getLogger("evcharge.ha")

ENV_FILE = Path.home() / ".hermes" / ".env"
# Warn about an unreachable HA at most this often - a 30 s cycle would otherwise
# fill the log with one line per cycle for a value nobody is acting on.
WARN_EVERY_S = 600.0
NON_NUMERIC = ("unknown", "unavailable", "none", "")


def load_env(env_file: Optional[str] = None) -> None:
    """Populate HASS_URL / HASS_TOKEN from the env file when not already set."""
    if os.environ.get("HASS_URL") and os.environ.get("HASS_TOKEN"):
        return
    path = Path(env_file).expanduser() if env_file else ENV_FILE
    if not path.exists():
        return
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key.startswith("HASS_") and not os.environ.get(key):
            os.environ[key] = val


class HaClient:
    """One entity, polled on demand, failure-tolerant."""

    def __init__(self, entity_id: str, base_url: Optional[str] = None,
                 token: Optional[str] = None, env_file: Optional[str] = None,
                 timeout: float = 3.0):
        self.entity_id = entity_id
        self.timeout = float(timeout)
        self._env_file = env_file
        self._base = (base_url or "").rstrip("/")
        self._token = token or ""
        self._warned_at = 0.0
        self._failures = 0

    # -- internals -------------------------------------------------------
    def _resolve(self) -> bool:
        if not self._base or not self._token:
            load_env(self._env_file)
            self._base = self._base or os.environ.get("HASS_URL", "").rstrip("/")
            self._token = self._token or os.environ.get("HASS_TOKEN", "")
        return bool(self._base and self._token)

    def _warn(self, msg: str, *args) -> None:
        now = time.time()
        if now - self._warned_at >= WARN_EVERY_S:
            self._warned_at = now
            LOG.warning(msg + " (%d consecutive failures)", *args, self._failures)

    # -- public ----------------------------------------------------------
    def get_state(self) -> Optional[Dict]:
        """{'value': float|None, 'raw': str, 'age_s': float|None} or None on failure."""
        if not self._resolve():
            self._failures += 1
            self._warn("HA reader for %s has no HASS_URL/HASS_TOKEN", self.entity_id)
            return None
        url = "%s/api/states/%s" % (self._base, self.entity_id)
        req = Request(url, headers={"Authorization": "Bearer " + self._token,
                                    "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - any failure is just "no value"
            self._failures += 1
            self._warn("HA read of %s failed: %s", self.entity_id, type(exc).__name__)
            return None
        self._failures = 0
        raw = str(data.get("state", ""))
        value: Optional[float] = None
        if raw.strip().lower() not in NON_NUMERIC:
            try:
                value = float(raw)
            except ValueError:
                value = None
        age: Optional[float] = None
        stamp = data.get("last_updated") or data.get("last_changed")
        if stamp:
            try:
                when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                age = max(0.0, time.time() - when.timestamp())
            except ValueError:
                age = None
        return {"value": value, "raw": raw, "age_s": age}