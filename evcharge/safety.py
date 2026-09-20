"""Charge-switching safety: detect and stop pathological on/off cycling.

A car's on-board charger is not a switch. Cycling the charging enable every few minutes -
which a control bug can do all night without anyone noticing - is exactly the kind of
thing that ends in a four-figure repair bill. So the app counts the transitions it really
applies to the wallbox over a sliding window, shows that number in the web UI, and when it
crosses a threshold it latches into a fault: turn the charge ON once, then write nothing
more to the wallbox, leaving the car charging steadily instead of being switched.

The fault latches on purpose and does not clear when the counter decays: whatever produced
the cycling may still be present, so releasing it is a human decision (the UI button, or a
restart).

Stdlib only and no I/O: the counting is pure, so it can be tested with injected times.
"""
import time
from collections import deque
from typing import Deque, Optional, Tuple

DEFAULT_THRESHOLD = 5
DEFAULT_WINDOW_S = 1800.0        # 30 minutes


class SwitchCounter:
    """Counts the charging on/off transitions applied in the last `window_s` seconds.

    Only real applied writes count (``alw=0`` / ``alw=1``), and only when the value
    actually *changes* - re-asserting the same state is not a transition and must not
    trip the safety net.
    """

    def __init__(self, threshold: int = DEFAULT_THRESHOLD,
                 window_s: float = DEFAULT_WINDOW_S):
        self.threshold = max(2, int(threshold))
        self.window_s = float(window_s)
        self._events: Deque[Tuple[float, str]] = deque()
        self._last: Optional[str] = None
        self.fault_since: Optional[float] = None
        self.fault_reason = ""

    # -- recording ------------------------------------------------------
    def note(self, command: str, now: Optional[float] = None) -> Optional[str]:
        """Record an applied ``alw=…`` write; return a fault reason if it just latched."""
        now = time.time() if now is None else now
        if command not in ("alw=0", "alw=1"):
            return None
        if self._last is not None and command != self._last:
            self._events.append((now, command))
        self._last = command
        return self._check(now)

    def _prune(self, now: float) -> None:
        cut = now - self.window_s
        while self._events and self._events[0][0] < cut:
            self._events.popleft()

    def _check(self, now: float) -> Optional[str]:
        if self.fault_since is not None:
            return None
        self._prune(now)
        if len(self._events) >= self.threshold:
            self.fault_since = now
            self.fault_reason = ("%d charging on/off changes within %.0f minutes"
                                 % (len(self._events), self.window_s / 60.0))
            return self.fault_reason
        return None

    # -- reading --------------------------------------------------------
    def count(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        self._prune(now)
        return len(self._events)

    @property
    def fault(self) -> bool:
        return self.fault_since is not None

    def clear(self) -> None:
        self.fault_since = None
        self.fault_reason = ""
        self._events.clear()
        self._last = None

    def as_state(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        return {
            "changes": self.count(now),
            "window_min": round(self.window_s / 60.0, 1),
            "threshold": self.threshold,
            "fault": self.fault,
            "fault_reason": self.fault_reason,
            "fault_min_ago": (None if self.fault_since is None
                              else round((now - self.fault_since) / 60.0, 1)),
        }