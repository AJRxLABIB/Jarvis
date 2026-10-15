"""Model health: a small circuit breaker and latency record per model.

A role model (fast / smart / coding) that keeps failing is skipped for a cooling-off period instead of being tried,
timed out and fallen back from on every request. The cool-off doubles with each further failure (up to 10 minutes)
and a single success resets it. The default model is never blocked here: if it is the only one, it must be tried
and its error reported.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

FAILURES_TO_OPEN = 3
BASE_COOLDOWN = 30.0
MAX_COOLDOWN = 600.0
LATENCY_WINDOW = 20


@dataclass
class _Record:
    consecutive_failures: int = 0
    total_failures: int = 0
    total_calls: int = 0
    open_until: float = 0.0
    last_error: str = ""
    latencies: deque = field(default_factory=lambda: deque(maxlen=LATENCY_WINDOW))


class ModelHealth:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._records: dict[str, _Record] = {}

    def _rec(self, model: str) -> _Record:
        return self._records.setdefault(model, _Record())

    def success(self, model: str, seconds: float) -> None:
        rec = self._rec(model)
        rec.total_calls += 1
        rec.consecutive_failures = 0
        rec.open_until = 0.0
        rec.latencies.append(max(0.0, seconds))

    def failure(self, model: str, error: str) -> None:
        rec = self._rec(model)
        rec.total_calls += 1
        rec.total_failures += 1
        rec.consecutive_failures += 1
        rec.last_error = error[:200]
        if rec.consecutive_failures >= FAILURES_TO_OPEN:
            steps = rec.consecutive_failures - FAILURES_TO_OPEN
            rec.open_until = self._clock() + min(MAX_COOLDOWN, BASE_COOLDOWN * (2 ** steps))

    def available(self, model: str) -> bool:
        rec = self._records.get(model)
        return rec is None or self._clock() >= rec.open_until

    def cooldown_left(self, model: str) -> float:
        rec = self._records.get(model)
        return 0.0 if rec is None else max(0.0, rec.open_until - self._clock())

    def summary(self) -> dict[str, dict]:
        """Per model: calls, failures, average latency and whether it is currently being skipped."""
        out = {}
        for name, rec in self._records.items():
            avg = sum(rec.latencies) / len(rec.latencies) if rec.latencies else None
            out[name] = {"calls": rec.total_calls, "failures": rec.total_failures,
                         "avg_seconds": round(avg, 2) if avg is not None else None,
                         "skipped_for": round(self.cooldown_left(name)), "last_error": rec.last_error}
        return out


HEADROOM = 1.15  # a model needs about its file size plus working memory


def total_memory_bytes() -> int | None:
    """Installed RAM, or None when it can't be read."""
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except Exception:
        return None


def could_ever_fit(size_bytes: int | None, total_bytes: int | None) -> bool:
    """False only when a model is bigger than this PC's whole RAM (with headroom): it can never load, so it is
    not worth a failed attempt. GPU memory is not counted, so this errs on the side of trying."""
    if not size_bytes or not total_bytes:
        return True
    return size_bytes * HEADROOM <= total_bytes
