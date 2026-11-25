"""The shared record the agents of one request write to and read from: who is doing what, what came out, what failed, what is still needed.

The orchestrator already validates a single plan (an agent only uses its own tools, a state-changing call appears once). What it could not see is
ACROSS several delegations in the same request - the model delegating the same side effect twice, two agents working on the same call at once, an
agent handing work back to the agent that handed it over. ``Blackboard`` is that missing shared view, one per request (run), in memory.

Typed entries (no free-form chatter between agents):
  doing    an agent started a call            result   it finished (ok or not, one-line summary)
  need     an agent is waiting for something  claim    a finding with its evidence label (verified / retrieved / inferred / predicted)

``begin`` is the gate: it answers *go*, *already done* (a state-changing call that succeeded earlier in this request is not repeated), *busy*
(the same call is in flight), *failed before* (the same call failed ``MAX_REPEAT_FAILURES`` times without anything changing), or *loop* (the
delegation chain revisits an agent / is too deep). Read-only calls may repeat. Pure logic; no model involved.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

MAX_DEPTH = 4
MAX_REPEAT_FAILURES = 2
MAX_ENTRIES = 200
GO, DONE, BUSY, FAILED_BEFORE, LOOP = "go", "already_done", "busy", "failed_before", "loop"


def call_key(tool: str, arguments: dict[str, Any] | None) -> str:
    return hashlib.sha256(f"{tool}|{json.dumps(arguments or {}, sort_keys=True, default=str)}".encode()).hexdigest()[:16]


@dataclass
class Entry:
    kind: str                      # doing | result | need | claim
    agent: str
    text: str
    key: str = ""
    ok: bool | None = None
    evidence: str = ""
    at: float = field(default_factory=time.time)


@dataclass
class Ticket:
    verdict: str
    reason: str = ""
    agent: str = ""
    key: str = ""
    tool: str = ""
    mutating: bool = False
    chain: tuple[str, ...] = ()

    @property
    def go(self) -> bool:
        return self.verdict == GO


class Blackboard:
    def __init__(self, task_id: str = "request") -> None:
        from app.core import interbrain

        self.task_id = task_id
        self.channel = interbrain.Channel()               # the typed InterBrain record of the same events (who told whom what kind of thing)
        self._lock = threading.Lock()
        self.entries: list[Entry] = []
        self._inflight: dict[str, str] = {}              # key -> agent
        self._done: dict[str, str] = {}                  # key -> "agent: summary" for successful state-changing calls
        self._failures: dict[str, int] = {}

    # ---- the gate ------------------------------------------------------------------------------------------------------------------------
    def begin(self, agent: str, tool: str, arguments: dict[str, Any] | None, *, mutating: bool, chain: tuple[str, ...] = ()) -> Ticket:
        key = call_key(tool, arguments)
        with self._lock:
            if agent in chain or len(chain) >= MAX_DEPTH:
                why = f"{agent} is already in the delegation chain ({' -> '.join(chain)})" if agent in chain else f"the delegation chain is {len(chain)} deep"
                return Ticket(LOOP, why, agent, key, tool, mutating, chain)
            if mutating and key in self._done:
                return Ticket(DONE, f"already done earlier in this request ({self._done[key]}); not repeated", agent, key, tool, mutating, chain)
            if key in self._inflight and (mutating or self._inflight[key] != agent):
                return Ticket(BUSY, f"{self._inflight[key]} is already doing exactly this", agent, key, tool, mutating, chain)
            if self._failures.get(key, 0) >= MAX_REPEAT_FAILURES:
                return Ticket(FAILED_BEFORE, f"this exact call already failed {self._failures[key]} times in this request; change something first",
                              agent, key, tool, mutating, chain)
            self._inflight[key] = agent
            self._add(Entry("doing", agent, f"{tool}", key))
            return Ticket(GO, "", agent, key, tool, mutating, chain + (agent,))

    def finish(self, ticket: Ticket, ok: bool, summary: str = "") -> None:
        if ticket.verdict != GO:
            return
        with self._lock:
            self._inflight.pop(ticket.key, None)
            if ok:
                self._failures.pop(ticket.key, None)
                if ticket.mutating:
                    self._done[ticket.key] = f"{ticket.agent}: {summary[:80] or ticket.tool}"
            else:
                self._failures[ticket.key] = self._failures.get(ticket.key, 0) + 1
            self._add(Entry("result", ticket.agent, f"{ticket.tool}: {summary[:200]}", ticket.key, ok))

    # ---- typed notes between agents ------------------------------------------------------------------------------------------------------
    def need(self, agent: str, what: str) -> None:
        with self._lock:
            self._add(Entry("need", agent, what[:200]))

    def claim(self, agent: str, text: str, evidence: str) -> None:
        if evidence not in ("verified", "retrieved", "inferred", "predicted"):
            raise ValueError(f"unknown evidence label: {evidence}")
        with self._lock:
            self._add(Entry("claim", agent, text[:240], evidence=evidence))

    def _add(self, entry: Entry) -> None:
        self.entries.append(entry)
        del self.entries[:-MAX_ENTRIES]
        self._post(entry)

    def _post(self, entry: Entry) -> None:
        """Mirror an entry as a validated InterBrain message. Never raises: the record must not be able to stop real work.
        doing -> TASK_ACCEPTED, result -> EVIDENCE_REPORT (ok) / FAILURE_REPORT, claim -> HYPOTHESIS or EVIDENCE_REPORT by its label, need -> DEPENDENCY_UPDATE."""
        try:
            from app.core import interbrain as IB

            kind = {"doing": "TASK_ACCEPTED", "need": "DEPENDENCY_UPDATE"}.get(entry.kind)
            status = "in_progress" if entry.kind == "doing" else "pending"
            if entry.kind == "result":
                kind, status = ("EVIDENCE_REPORT", "done") if entry.ok else ("FAILURE_REPORT", "failed")
            elif entry.kind == "claim":
                kind = "EVIDENCE_REPORT" if entry.evidence in ("verified", "retrieved") else "HYPOTHESIS"
            if kind is None:
                return
            refs = [entry.key] if entry.key else ([f"label:{entry.evidence}"] if entry.evidence else [])
            self.channel.post(IB.Message(self.task_id, entry.agent or "agent", "supervisor", kind, findings=[entry.text[:200]], evidence=refs, status=status))
        except Exception:
            pass

    def timeline(self) -> str:
        return self.channel.timeline(self.task_id)

    # ---- what another agent can read -----------------------------------------------------------------------------------------------------
    def view(self, for_agent: str = "") -> dict[str, Any]:
        with self._lock:
            return {
                "in_progress": [{"agent": a, "key": k} for k, a in self._inflight.items() if a != for_agent],
                "done": list(self._done.values()),
                "failed": [e.text for e in self.entries if e.kind == "result" and e.ok is False][-5:],
                "needs": [f"{e.agent}: {e.text}" for e in self.entries if e.kind == "need"][-5:],
                "claims": [f"[{e.evidence}] {e.text}" for e in self.entries if e.kind == "claim"][-8:],
                "messages": self.channel.timeline(self.task_id).splitlines()[-8:],
            }

    def summary(self) -> str:
        v = self.view()
        lines = []
        if v["done"]:
            lines.append("done: " + "; ".join(v["done"]))
        if v["failed"]:
            lines.append("failed: " + "; ".join(v["failed"]))
        if v["needs"]:
            lines.append("waiting for: " + "; ".join(v["needs"]))
        return " | ".join(lines)


_BOARDS: dict[str, Blackboard] = {}
_ORDER: list[str] = []


def for_run(run_id: str | None = None) -> Blackboard:
    """The blackboard of the current request (or of ``run_id``); a new one per run, the last 20 kept. Outside a run: a fresh throwaway board."""
    if run_id is None:
        from app.core import runs

        current = runs.current()
        run_id = current.id if current is not None else ""
    if not run_id:
        return Blackboard()
    if run_id not in _BOARDS:
        _BOARDS[run_id] = Blackboard(run_id)
        _ORDER.append(run_id)
        while len(_ORDER) > 20:
            _BOARDS.pop(_ORDER.pop(0), None)
    return _BOARDS[run_id]
