"""One lifecycle for every action (voice, text, gesture, phone, automation): who asked, what exactly, what was approved.

Pure logic - no I/O. ``ToolExecutor`` (app/agent/tool_manager.py) is the single execution path and drives this record; the
transitions are mirrored into the existing audit log (no second database). What it adds over the executor's checks:

* a validated state machine - an illegal jump (e.g. RUNNING without APPROVED) raises instead of being silently accepted;
* an approval *bound* to a digest of (tool, canonical arguments, target, mission, risk level) with an expiry - if anything
  material changes after the user said yes, the approval no longer covers the action and it is not run;
* an honest UNCERTAIN end state for "may have happened" (timeout, lost answer) so a retry is never blind;
* an idempotency key (same call in the same request = same key).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class State(str, Enum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"          # refused by policy or by the user
    CANCELLED = "cancelled"
    FAILED = "failed"              # ran (or tried) and the real result was not what was asked
    EXPIRED = "expired"            # the approval or deadline ran out
    UNCERTAIN = "uncertain"        # may or may not have happened: inspect before any retry


TERMINAL = frozenset({State.SUCCEEDED, State.REJECTED, State.CANCELLED, State.FAILED, State.EXPIRED, State.UNCERTAIN})

_ALLOWED: dict[State, frozenset[State]] = {
    State.PROPOSED: frozenset({State.VALIDATED, State.REJECTED, State.CANCELLED, State.FAILED}),
    State.VALIDATED: frozenset({State.AWAITING_APPROVAL, State.APPROVED, State.REJECTED, State.CANCELLED, State.EXPIRED}),
    State.AWAITING_APPROVAL: frozenset({State.APPROVED, State.REJECTED, State.CANCELLED, State.EXPIRED}),
    State.APPROVED: frozenset({State.RUNNING, State.REJECTED, State.CANCELLED, State.EXPIRED}),
    State.RUNNING: frozenset({State.VERIFYING, State.SUCCEEDED, State.FAILED, State.CANCELLED, State.UNCERTAIN}),
    State.VERIFYING: frozenset({State.SUCCEEDED, State.FAILED, State.UNCERTAIN, State.CANCELLED}),
}


class InvalidTransition(RuntimeError):
    pass


def digest(tool: str, arguments: dict[str, Any], *, target_device: str = "pc", target_app: str = "", mission: str = "",
           level: int = 0) -> str:
    """Stable fingerprint of everything an approval must cover. Key order and whitespace do not matter; any value does."""
    body = json.dumps({"t": tool, "a": arguments, "d": target_device, "p": target_app, "m": mission, "l": int(level)},
                      sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Approval:
    """A yes for exactly one digest, valid until ``expires_at`` (monotonic seconds)."""

    digest: str
    given_by: str                  # "user", "standing_grant", "approved_plan", "direct_command", "policy"
    expires_at: float
    at: float = field(default_factory=time.monotonic)

    def covers(self, other_digest: str, now: float | None = None) -> tuple[bool, str]:
        if (now if now is not None else time.monotonic()) > self.expires_at:
            return False, "the approval expired"
        if other_digest != self.digest:
            return False, "the action changed after it was approved"
        return True, ""


@dataclass
class ActionRecord:
    tool: str
    arguments: dict[str, Any]
    level: int = 0
    origin_device: str = "pc"
    input_method: str = "text"             # text | voice | gesture | phone | automation | agent | cli
    target_device: str = "pc"
    target_app: str = ""
    mission: str = ""
    capability: str = ""
    deadline_s: float | None = None
    id: str = field(default_factory=lambda: secrets.token_hex(6))
    state: State = State.PROPOSED
    approval: Approval | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    recovery: str = ""
    history: list[tuple[float, str, str]] = field(default_factory=list)       # (monotonic, state, note)
    on_change: Callable[["ActionRecord", State, str], None] | None = field(default=None, repr=False, compare=False)

    @property
    def digest(self) -> str:
        return digest(self.tool, self.arguments, target_device=self.target_device, target_app=self.target_app,
                      mission=self.mission, level=self.level)

    @property
    def idempotency_key(self) -> str:
        return f"{self.mission or 'adhoc'}:{self.digest[:16]}"

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL

    def to(self, new: State, note: str = "") -> "ActionRecord":
        if new == self.state:
            return self
        if new not in _ALLOWED.get(self.state, frozenset()):
            raise InvalidTransition(f"{self.state.value} -> {new.value} is not allowed")
        if new == State.RUNNING:
            if self.approval is None:
                raise InvalidTransition("running needs an approval (even an automatic one is recorded)")
            ok, why = self.approval.covers(self.digest)
            if not ok:
                raise InvalidTransition(why)
        self.state = new
        self.history.append((time.monotonic(), new.value, note))
        if self.on_change is not None:
            try:
                self.on_change(self, new, note)
            except Exception:           # recording must never break the action
                pass
        return self

    def approve(self, given_by: str, ttl_s: float = 120.0) -> "ActionRecord":
        """Bind an approval to the record AS IT IS NOW, then move to APPROVED."""
        self.approval = Approval(self.digest, given_by, time.monotonic() + ttl_s)
        return self.to(State.APPROVED, given_by)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "tool": self.tool, "state": self.state.value, "level": self.level, "input": self.input_method,
                "device": self.target_device, "mission": self.mission, "idempotency_key": self.idempotency_key,
                "approved_by": self.approval.given_by if self.approval else None, "recovery": self.recovery,
                "steps": [(s, n) for _, s, n in self.history]}


class Ledger:
    """The last N actions of this run of the app (a view for the UI / tool; the audit log is the durable record)."""

    def __init__(self, limit: int = 200) -> None:
        self._items: "OrderedDict[str, ActionRecord]" = OrderedDict()
        self.limit = limit

    def add(self, record: ActionRecord) -> ActionRecord:
        self._items[record.id] = record
        while len(self._items) > self.limit:
            self._items.popitem(last=False)
        return record

    def get(self, action_id: str) -> ActionRecord | None:
        return self._items.get(action_id)

    def recent(self, n: int = 20) -> list[ActionRecord]:
        return list(self._items.values())[-n:][::-1]

    def uncertain(self) -> list[ActionRecord]:
        return [r for r in self._items.values() if r.state == State.UNCERTAIN]

    def in_flight(self) -> list[ActionRecord]:
        return [r for r in self._items.values() if not r.finished]

    def duplicate_of(self, record: ActionRecord) -> ActionRecord | None:
        """An earlier action with the same idempotency key that succeeded or may have (so a retry would repeat it)."""
        for other in self._items.values():
            if other is not record and other.idempotency_key == record.idempotency_key and \
                    other.state in (State.SUCCEEDED, State.UNCERTAIN, State.RUNNING, State.VERIFYING):
                return other
        return None


LEDGER = Ledger()
