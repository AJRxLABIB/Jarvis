"""InterBrain protocol and the debate engine: typed messages between specialist roles, and an evidence-weighted way to pick between competing answers.

Built on the existing agent / blackboard infrastructure (``core/agents.py``, ``core/blackboard.py``): this adds the missing CONTRACT, not another agent system.

* ``Message`` / ``validate`` - every message has an id, task id, sender, recipient, type, objective, evidence references, findings, assumptions,
  confidence, uncertainty, dependencies, a recommended next action and a status. Rules that keep it honest: a TASK_COMPLETED message must cite
  evidence (no "done" without something to check); a CHALLENGE must say what it objects to; confidence is 0-1; unknown types / statuses are refused.
* ``Channel`` - a bounded in-memory log per task (oldest dropped), readable as a timeline of WHO told WHOM WHAT KIND of thing. It never stores
  private reasoning: only findings, evidence references and decisions.
* ``debate`` - proposals with evidence labels and challenges. Score = evidence weight (verified 3, retrieved 2, inferred 1, predicted 0.5) scaled by
  stated confidence, minus unresolved challenges. A proposal with NO evidence scores 0 however confident it sounds, and an unresolved severe challenge
  keeps the result "contested". The result is a recommendation with its reasons, never an action.
Honest status: pure logic + tests. It is not yet wired into the live mission loop; the orchestrator can adopt it per task.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import dataclass, field

TYPES = ("TASK_REQUEST", "TASK_ACCEPTED", "REASONING_RESULT", "EVIDENCE_REPORT", "HYPOTHESIS", "CHALLENGE", "CORRECTION", "DEPENDENCY_UPDATE",
         "VERIFICATION_RESULT", "FAILURE_REPORT", "RECOVERY_REQUEST", "TASK_COMPLETED")
STATUSES = ("pending", "accepted", "in_progress", "done", "failed", "rejected")
EVIDENCE_WEIGHT = {"verified": 3.0, "retrieved": 2.0, "inferred": 1.0, "predicted": 0.5}
MAX_LOG = 200
_ids = itertools.count(1)


class ProtocolError(ValueError):
    pass


@dataclass
class Message:
    task_id: str
    sender: str
    recipient: str
    type: str
    objective: str = ""
    findings: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)          # references (memory #, file, tool result id), not contents
    assumptions: list[str] = field(default_factory=list)
    confidence: float | None = None
    uncertainty: str = ""
    dependencies: list[str] = field(default_factory=list)
    next_action: str = ""
    status: str = "pending"
    context_ref: str = ""
    id: str = ""
    at: float = 0.0


def validate(m: Message) -> Message:
    if m.type not in TYPES:
        raise ProtocolError(f"unknown message type '{m.type}'")
    if m.status not in STATUSES:
        raise ProtocolError(f"unknown status '{m.status}'")
    if not (m.task_id and m.sender and m.recipient):
        raise ProtocolError("task_id, sender and recipient are required")
    if m.confidence is not None and not 0.0 <= m.confidence <= 1.0:
        raise ProtocolError("confidence must be between 0 and 1")
    if m.type == "TASK_COMPLETED" and not m.evidence:
        raise ProtocolError("a completion claim must cite evidence")
    if m.type == "CHALLENGE" and not (m.findings or m.objective):
        raise ProtocolError("a challenge must say what it objects to")
    if m.type == "VERIFICATION_RESULT" and m.status not in ("done", "failed"):
        raise ProtocolError("a verification result is done (passed) or failed")
    if not m.id:
        m.id = f"m{next(_ids)}"
    m.at = m.at or time.time()
    return m


class Channel:
    def __init__(self, limit: int = MAX_LOG) -> None:
        self.log: deque[Message] = deque(maxlen=limit)

    def post(self, m: Message) -> Message:
        self.log.append(validate(m))
        return m

    def for_task(self, task_id: str) -> list[Message]:
        return [m for m in self.log if m.task_id == task_id]

    def open_challenges(self, task_id: str) -> list[Message]:
        msgs = self.for_task(task_id)
        answered = {m.context_ref for m in msgs if m.type == "CORRECTION"}
        return [m for m in msgs if m.type == "CHALLENGE" and m.id not in answered]

    def timeline(self, task_id: str) -> str:
        return "\n".join(f"{m.sender} -> {m.recipient}: {m.type}" + (f" - {m.findings[0][:80]}" if m.findings else (f" - {m.objective[:80]}" if m.objective else ""))
                         + (f" [{m.status}]" if m.status != "pending" else "") for m in self.for_task(task_id)) or "No messages for that task."


@dataclass
class Proposal:
    id: str
    text: str
    evidence: list[str] = field(default_factory=list)          # labels: verified / retrieved / inferred / predicted
    confidence: float = 0.5
    by: str = ""


@dataclass
class Challenge:
    target: str
    text: str
    severity: int = 1                                          # 1 minor, 2 serious, 3 blocking
    resolved: bool = False


@dataclass
class Ranked:
    proposal: Proposal
    score: float
    open_challenges: list[Challenge]


@dataclass
class Verdict:
    status: str                                                # clear | contested | insufficient
    winner: Proposal | None
    ranking: list[Ranked]
    reasons: list[str]

    def text(self) -> str:
        if self.winner is None:
            return "No proposal is supported by evidence yet: " + "; ".join(self.reasons)
        head = {"clear": "Recommended", "contested": "Leading but CONTESTED"}.get(self.status, "Weak")
        return f"{head}: {self.winner.text[:120]} - " + "; ".join(self.reasons)


def debate(proposals: list[Proposal], challenges: list[Challenge] | None = None) -> Verdict:
    challenges = challenges or []
    ranked = []
    for p in proposals:
        weight = sum(EVIDENCE_WEIGHT.get(e, 0.0) for e in p.evidence)
        score = weight * (0.5 + 0.5 * max(0.0, min(1.0, p.confidence)))
        open_ = [c for c in challenges if c.target == p.id and not c.resolved]
        score -= sum(2.0 * c.severity for c in open_)
        ranked.append(Ranked(p, round(score, 2), open_))
    ranked.sort(key=lambda r: (-r.score, r.proposal.id))
    if not ranked or ranked[0].score <= 0:
        return Verdict("insufficient", None, ranked, ["no proposal has evidence that outweighs its open challenges" if ranked else "nothing was proposed"])
    top = ranked[0]
    reasons = [f"score {top.score} from evidence {', '.join(top.proposal.evidence) or 'none'}"]
    margin = top.score - (ranked[1].score if len(ranked) > 1 else 0.0)
    if top.open_challenges:
        reasons.append(f"{len(top.open_challenges)} unresolved challenge(s): " + "; ".join(c.text[:60] for c in top.open_challenges[:2]))
    if any(c.severity >= 3 for c in top.open_challenges) or margin < 1.0 and len(ranked) > 1:
        if margin < 1.0 and len(ranked) > 1:
            reasons.append("the runner-up is almost as well supported")
        return Verdict("contested", top.proposal, ranked, reasons)
    return Verdict("clear", top.proposal, ranked, reasons)
