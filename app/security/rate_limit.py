"""Sliding-window limits on tool calls: a runaway model loop can't fire off
hundreds of actions. Side-effect actions (level >= 1) have a tighter limit."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from app.security.permissions import RiskLevel


class RateLimiter:
    def __init__(self, max_calls: int = 30, max_actions: int = 10, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_calls, self.max_actions, self.window = max_calls, max_actions, window
        self._clock = clock
        self._calls: deque[float] = deque()
        self._actions: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, queue: deque[float], now: float) -> None:
        while queue and now - queue[0] > self.window:
            queue.popleft()

    def allow(self, level: RiskLevel) -> bool:
        with self._lock:
            now = self._clock()
            self._trim(self._calls, now)
            self._trim(self._actions, now)
            if len(self._calls) >= self.max_calls:
                return False
            if level >= RiskLevel.MODERATE and len(self._actions) >= self.max_actions:
                return False
            self._calls.append(now)
            if level >= RiskLevel.MODERATE:
                self._actions.append(now)
            return True
