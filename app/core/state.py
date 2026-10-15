"""The assistant's state as an explicit state machine (it must never silently stay stuck).

States (the HUD shows them): IDLE, LISTENING, THINKING, EXECUTING, WAITING_FOR_PERMISSION, SPEAKING, ERROR.

* Every change goes through ``to()``: it is thread-safe, logged with the reason, and kept in a short
  history (for diagnostics).
* Transitions have rules. One that breaks them is still applied - refusing it would leave the
  assistant stuck in the old state, which is worse - but it is logged as ``state_unexpected`` so the
  bug shows up.
* A watchdog: each busy state has a maximum time. ``check()`` (called every few seconds) asks the host
  what the real state is; when the machine has been in a busy state too long and the host says it
  isn't busy any more, it goes back to what's true and logs ``state_watchdog``.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("core.state")


class AgentState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    EXECUTING = "EXECUTING"
    WAITING_FOR_PERMISSION = "WAITING_FOR_PERMISSION"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"


S = AgentState
# from -> the states it may go to
TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    S.IDLE: frozenset({S.LISTENING, S.THINKING, S.EXECUTING, S.SPEAKING, S.ERROR, S.WAITING_FOR_PERMISSION}),
    S.LISTENING: frozenset({S.IDLE, S.THINKING, S.SPEAKING, S.ERROR, S.EXECUTING}),
    S.THINKING: frozenset({S.EXECUTING, S.SPEAKING, S.IDLE, S.ERROR, S.WAITING_FOR_PERMISSION, S.LISTENING}),
    S.EXECUTING: frozenset({S.THINKING, S.WAITING_FOR_PERMISSION, S.SPEAKING, S.IDLE, S.ERROR, S.LISTENING}),
    S.WAITING_FOR_PERMISSION: frozenset({S.EXECUTING, S.THINKING, S.IDLE, S.ERROR, S.SPEAKING, S.LISTENING}),
    S.SPEAKING: frozenset({S.IDLE, S.LISTENING, S.THINKING, S.EXECUTING, S.ERROR, S.WAITING_FOR_PERMISSION}),
    S.ERROR: frozenset({S.IDLE, S.LISTENING, S.THINKING, S.SPEAKING, S.EXECUTING}),
}
# the longest a state may last before the watchdog checks whether it's still true (seconds)
MAX_SECONDS: dict[AgentState, float] = {
    S.LISTENING: 90.0, S.THINKING: 240.0, S.EXECUTING: 660.0, S.WAITING_FOR_PERMISSION: 120.0, S.SPEAKING: 600.0,
}


@dataclass(frozen=True)
class Transition:
    at: float
    old: AgentState
    new: AgentState
    reason: str
    expected: bool


class StateMachine:
    def __init__(self, on_change: Callable[[AgentState], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._state = S.IDLE
        self._since = clock()
        self._clock = clock
        self.on_change = on_change
        self.history: deque[Transition] = deque(maxlen=60)

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def seconds_in_state(self) -> float:
        return self._clock() - self._since

    def to(self, new: AgentState | str, reason: str = "") -> bool:
        """Change state (from any thread). Returns True if it changed."""
        new = AgentState(new)
        with self._lock:
            old = self._state
            if new == old:
                return False
            expected = new in TRANSITIONS[old]
            self._state, self._since = new, self._clock()
            self.history.append(Transition(self._since, old, new, reason, expected))
        if not expected:
            log_event(_log, "state_unexpected", old=old.value, new=new.value, reason=reason)
        if self.on_change is not None:
            try:
                self.on_change(new)
            except Exception:
                _log.warning("state listener failed", exc_info=True)
        return True

    def check(self, truth: Callable[[], AgentState | str]) -> bool:
        """Watchdog. ``truth()`` says what the state really is; used only when a state has lasted too long."""
        state = self._state
        limit = MAX_SECONDS.get(state)
        if limit is None or self.seconds_in_state < limit:
            return False
        try:
            real = AgentState(truth())
        except Exception:
            real = S.IDLE
        if real == state:
            with self._lock:  # it really is still busy: look again after another full period
                self._since = self._clock()
            return False
        log_event(_log, "state_watchdog", stuck=state.value, seconds=round(self.seconds_in_state), now=real.value)
        return self.to(real, "watchdog")

    def recent(self, count: int = 10) -> list[str]:
        return [f"{t.old.value}->{t.new.value} ({t.reason or '-'})" + ("" if t.expected else " !")
                for t in list(self.history)[-count:]]
