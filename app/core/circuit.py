"""Circuit breakers for tools that talk to something outside Jarvis (the internet, a phone, a smart-home hub).

When a service keeps failing for a TRANSIENT reason (time-outs, connection errors, rate limits - the ``retry`` classifier decides), calling it again and
again only makes every request slow. After ``OPEN_AFTER`` such failures in a row the breaker for that tool OPENS: calls return at once with a clear
"paused" message for a cool-down (60 s, doubling to at most 15 minutes). After the cool-down ONE trial call is let through (half-open); success closes
the breaker, failure opens it again for longer.
Not counted: failures that are your settings, a permission, a bad argument or a "not found" - a breaker is for an unreachable service, not a wrong request.
Never blocked: a command you gave directly (it is always tried, and counts as the trial). State is in memory only and resets when Jarvis restarts.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

OPEN_AFTER = 3
BASE_COOLDOWN, MAX_COOLDOWN = 60.0, 900.0


@dataclass
class _State:
    failures: int = 0
    opened: int = 0               # how many times in a row it has opened (drives the cool-down)
    open_until: float = 0.0
    trial: bool = False           # a half-open trial call is in flight
    last_error: str = ""


class Breakers:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock, self._lock, self._s = clock, threading.Lock(), {}

    def allow(self, name: str, direct: bool = False) -> tuple[bool, str]:
        """(may the call go ahead, why not). ``direct`` = your own command: always tried."""
        with self._lock:
            st = self._s.get(name)
            if st is None or st.open_until == 0.0:
                return True, ""
            now = self.clock()
            if now >= st.open_until or direct:
                if st.trial and not direct:
                    return False, f"'{name}' is being re-tried right now; wait for that result"
                st.trial = True
                return True, ""
            left = int(st.open_until - now)
            return False, (f"'{name}' is paused for about {left} s: it failed {st.failures} times in a row ({st.last_error or 'no answer'}). "
                           "Ask again later, or give the command yourself to try now.")

    def record(self, name: str, ok: bool, transient: bool = False, error: str = "") -> None:
        with self._lock:
            st = self._s.setdefault(name, _State())
            st.trial = False
            if ok:
                self._s.pop(name, None)
                return
            if not transient:
                return                                             # a wrong request says nothing about the service
            st.failures += 1
            st.last_error = (error or "")[:80]
            if st.failures >= OPEN_AFTER:
                st.opened += 1
                st.open_until = self.clock() + min(MAX_COOLDOWN, BASE_COOLDOWN * 2 ** (st.opened - 1))

    def reset(self, name: str | None = None) -> int:
        with self._lock:
            n = len(self._s) if name is None else int(name in self._s)
            if name is None:
                self._s.clear()
            else:
                self._s.pop(name, None)
            return n

    def snapshot(self) -> list[dict]:
        now = self.clock()
        with self._lock:
            return [{"tool": k, "failures": s.failures, "paused_s": max(0, int(s.open_until - now)) if s.open_until else 0, "last_error": s.last_error} for k, s in sorted(self._s.items())]


BREAKERS = Breakers()


def describe(rows: list[dict]) -> str:
    if not rows:
        return "No service is paused and none is failing repeatedly."
    return "\n".join(f"{r['tool']}: " + (f"PAUSED for {r['paused_s']} s" if r["paused_s"] else "failing, not paused yet") + f" - {r['failures']} failure(s) in a row; last: {r['last_error'] or 'n/a'}"
                     for r in rows)
