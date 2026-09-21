"""Charge-switching safety: detect and stop pathological on/off cycling.

A car's on-board charger is not a switch. Cycling the charging enable every few minutes -
which a control bug can do all night without anyone noticing - is exactly the kind of
thing that ends in a four-figure repair bill. So the app counts the *stops* it really
applies to the wallbox over a sliding window, shows that number in the web UI, and when it
crosses a threshold it latches into a fault: turn the charge ON once, then write nothing
more to the wallbox, leaving the car charging steadily instead of being switched.

Stops, not edges: every start is the counterpart of a stop, so counting both directions
reported one interrupted session as two events and halved the tolerance the owner set. The
stop is also the edge that actually hurts - it is the moment the car's charger loses its
supply - while a start only puts it back. Current-limit changes (``amx=…``) are not
switching and never count.

The fault latches on purpose and does not clear when the counter decays: whatever produced
the cycling may still be present, so releasing it is a human decision. Two actions count as
one, by the owner's decision: the UI button, and a **service restart** - the counter lives in
memory only, so a restart clears the fault by construction, and that is wanted (whoever
restarts has looked at the cause first). What is *not* wanted is the opposite: that the latch
releases itself while the app keeps running.

Stdlib only and no I/O: the counting is pure, so it can be tested with injected times.
"""
import time
from collections import deque
from typing import Deque, Optional, Tuple

DEFAULT_THRESHOLD = 5
DEFAULT_WINDOW_S = 1800.0        # 30 minutes


class SwitchCounter:
    """Counts the charging *stops* applied in the last `window_s` seconds.

    Only real applied writes count (``alw=0`` / ``alw=1``), and only stops: an ``alw=0``
    that actually follows an ``alw=1``. Re-asserting the same state is not a transition,
    and ``amx=…`` (the current limit) is not switching at all.
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
        """Record an applied ``alw=…`` write; return a fault reason if it just latched.

        Only **stops** count - an ``alw=0`` that really follows an ``alw=1``. Every start is
        the counterpart of a stop, so counting both directions reported one interrupted
        session as two events and halved the tolerance. The stop is also the edge that
        hurts: it is the moment the car's on-board charger loses its supply mid-session.
        ``amx=…`` never counts (re-limiting is not switching), and re-asserting a state is
        not a transition at all.
        """
        now = time.time() if now is None else now
        if command not in ("alw=0", "alw=1"):
            return None
        if command == "alw=0" and self._last == "alw=1":
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
            self.fault_reason = ("%d charging stops within %.0f minutes"
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