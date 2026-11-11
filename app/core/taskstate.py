"""Task lifecycle rules shared by missions, workflows and agents: valid state transitions and a history.

A task never becomes ``completed`` because a model said so. The path is explicit:

    planned -> running -> verifying -> completed

``paused`` (waiting for you, a checkpoint, a failed step, a restart), ``failed``, ``cancelled`` and
``rolled_back`` are the other states. :func:`check_transition` enforces the table; :func:`verify_completion`
is the deterministic check that runs in the ``verifying`` state.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

MISSION_STATES = ("planned", "running", "verifying", "paused", "completed", "failed", "cancelled", "waiting",
                  "rolled_back")
TERMINAL = frozenset({"completed", "cancelled", "rolled_back"})

# state -> states it may move to (staying in the same state is always allowed)
TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"running", "paused", "cancelled", "failed", "waiting"}),
    "waiting": frozenset({"running", "paused", "cancelled", "failed"}),
    "running": frozenset({"verifying", "paused", "failed", "cancelled", "waiting"}),
    "verifying": frozenset({"completed", "paused", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled", "failed", "rolled_back", "planned", "waiting"}),
    "failed": frozenset({"running", "paused", "cancelled", "rolled_back", "planned"}),
    "completed": frozenset({"rolled_back"}),
    "cancelled": frozenset({"rolled_back"}),
    "rolled_back": frozenset(),
}


class InvalidTransition(ValueError):
    """A task tried to move between states the lifecycle doesn't allow."""

    def __init__(self, old: str, new: str) -> None:
        super().__init__(f"a task can't go from '{old}' to '{new}'")
        self.old, self.new = old, new


def can_transition(old: str, new: str) -> bool:
    return old == new or new in TRANSITIONS.get(old, frozenset())


def check_transition(old: str, new: str) -> None:
    if new not in MISSION_STATES:
        raise InvalidTransition(old, new)
    if not can_transition(old, new):
        raise InvalidTransition(old, new)


class _StepLike(Protocol):
    n: int
    title: str
    state: str
    result: str


def verify_completion(steps: Iterable[_StepLike]) -> tuple[bool, str]:
    """The ``verifying`` check: every step must have finished (done) or been skipped on purpose.

    Returns (ok, note). Skipped steps are reported, never hidden. Per-step verification of the real world
    (the file exists, the volume changed) already happened in the executor when each step ran.
    """
    steps = list(steps)
    open_steps = [s for s in steps if s.state not in ("done", "skipped", "undone")]
    if open_steps:
        names = ", ".join(f"{s.n} ({s.title})" for s in open_steps[:3])
        return False, f"{len(open_steps)} step(s) not finished: {names}"
    done = sum(1 for s in steps if s.state == "done")
    if steps and done == 0:
        return False, "no step actually ran"
    skipped = [s for s in steps if s.state == "skipped"]
    if skipped:
        return True, f"completed with {len(skipped)} step(s) skipped: " + ", ".join(str(s.n) for s in skipped)
    return True, ""


def display_state(state: str, note: str = "", step_states: list[str] | None = None) -> str:
    """The mission state in plain words, splitting "paused" / "waiting" by WHY: awaiting approval, blocked, recovering. The stored state is unchanged."""
    low, steps = (note or "").lower(), step_states or []
    if state in ("paused", "waiting"):
        if "checkpoint" in low or "go-ahead" in low or "waiting" in steps or "approval" in low:
            return "awaiting approval"
        if "failed" in steps or low.startswith("paused at step"):
            return "blocked"
        if "interrupted" in low or "restarted" in low:
            return "interrupted (resume to continue)"
        if "switched off" in low or "emergency" in low:
            return "paused (automations are off)"
        return "paused"
    if state == "running" and any(s == "running" for s in steps) and "retry" in low:
        return "recovering"
    return {"planned": "ready", "verifying": "verifying the result"}.get(state, state)
