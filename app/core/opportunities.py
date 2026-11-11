"""Opportunity discovery: a short, evidence-backed list of improvements found in what Jarvis already records - never a stream of generic advice.

Sources (each optional; one that cannot be read is named under "could not check"): repeated tool sequences (the Experience Compiler), tools that keep
failing (capability gaps), missions paused for days, active goals with no tasks, overdue promises, and Jarvis's own prediction track record.
Every opportunity carries: the evidence (counts and dates from the records), the expected benefit (stated as an upper bound or an estimate), the
complexity, the risk, and one concrete next action. Ranking = evidence strength x benefit; at most ``LIMIT`` are shown; anything you dismiss stays
dismissed (the radar feedback store). Nothing is acted on here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

LIMIT = 7
STALL_DAYS = 3


@dataclass
class Opportunity:
    key: str
    kind: str
    title: str
    evidence: str
    benefit: str
    complexity: str          # low | medium | high
    risk: str
    next_action: str
    score: float


def from_workflows(proposals: list[Any]) -> list[Opportunity]:
    return [Opportunity(f"opp:workflow:{p.id}", "automation", f"Automate '{p.name}'", f"done {p.runs} times on {p.days} days, last {str(p.last_seen)[:10]}",
                        f"about {p.seconds_saved:.0f} s of tool time per run (estimate)" if p.seconds_saved >= 5 else "fewer repeated requests", "low",
                        "none until you approve the workflow; it keeps every normal confirmation", "say 'show workflow proposals' and approve it",
                        min(10.0, p.runs * 1.0 + p.days)) for p in proposals]


def from_failures(gaps: list[Any]) -> list[Opportunity]:
    return [Opportunity(f"opp:failing:{g.tool}:{g.cause}", "recurring error", f"'{g.tool}' keeps failing", f"failed {g.count} of {g.calls} calls in 14 days; e.g. {g.example or 'n/a'}",
                        g.benefit, "medium", g.security, g.approach, min(10.0, g.count * 1.5)) for g in gaps if g.count >= 3]


def from_missions(rows: list[dict], now: datetime) -> list[Opportunity]:
    out = []
    for r in rows:
        try:
            age = (now - datetime.fromisoformat(str(r["updated_at"]).replace("Z", "+00:00"))).days
        except (ValueError, TypeError):
            continue
        if r.get("state") == "paused" and age >= STALL_DAYS:
            out.append(Opportunity(f"opp:mission:{r['id']}", "unfinished work", f"Mission '{r['title']}' is paused", f"paused for {age} days" + (f" ({r['note']})" if r.get("note") else ""),
                                   "finishes work already started", "low", "resuming re-checks every step's approval", f"say 'resume mission {r['id']}' or cancel it", min(10.0, 3 + age * 0.5)))
    return out


def from_goals(goals: list[dict]) -> list[Opportunity]:
    return [Opportunity(f"opp:goal:{g['id']}", "missing plan", f"Goal '{g['title']}' has no tasks", "an active goal with no tasks has no measurable progress",
                        "makes progress and deadline risk visible", "low", "none", "say 'plan my goal' to break it into tasks", 4.0) for g in goals if not g.get("tasks")]


def from_commitments(items: list[Any], today: str) -> list[Opportunity]:
    late = [c for c in items if c.get("kind") == "i_owe" and c.get("due") and c["due"] < today]
    if not late:
        return []
    return [Opportunity("opp:promises:overdue", "follow-up", f"{len(late)} promise(s) you made are overdue", "; ".join(f"{c['text'][:50]} (due {c['due']})" for c in late[:3]),
                        "keeps commitments to other people", "low", "none: replies are only drafted, never sent without you", "say 'what did I promise'", 6.0 + len(late))]


def from_calibration(cal: dict[str, Any]) -> list[Opportunity]:
    if not cal.get("enough") or cal.get("verdict") != "overconfident":
        return []
    return [Opportunity("opp:calibration", "self-knowledge", "My predictions are overconfident", f"said {cal['stated']:.0%} on average, right {cal['hit_rate']:.0%} over {cal['resolved']} resolved",
                        "more honest confidence in future answers", "low", "none", "say 'how good are your predictions'", 5.0)]


def rank(items: list[Opportunity], blocked: Callable[[str], str] = lambda k: "", limit: int = LIMIT) -> tuple[list[Opportunity], int]:
    keep = [o for o in items if not blocked(o.key)]
    return sorted(keep, key=lambda o: -o.score)[:limit], len(items) - len(keep)


def collect(db: Any, now: datetime | None = None) -> tuple[list[Opportunity], list[str]]:
    now = now or datetime.now(timezone.utc)
    out: list[Opportunity] = []
    failed: list[str] = []

    def source(name: str, fn: Callable[[], list[Opportunity]]) -> None:
        try:
            out.extend(fn())
        except Exception:
            failed.append(name)

    def workflows():
        from app.core import experience

        return from_workflows(experience.propose(db))

    def failures():
        from app.core import capability_gaps

        return from_failures(capability_gaps.proposals(db))

    def missions():
        with db.connect() as conn:
            return from_missions([dict(r) for r in conn.execute("SELECT id, title, state, note, updated_at FROM missions WHERE state = 'paused'")], now)

    def goals():
        from app.core import deadlines

        return from_goals(deadlines.load(db)[1])

    def promises():
        from app.core import commitments

        return from_commitments(commitments.Ledger(db).items(), now.date().isoformat())

    def calib():
        from app.core import predictions

        return from_calibration(predictions.calibration(predictions.Ledger(db).items(limit=500)))

    for name, fn in (("repeated workflows", workflows), ("failing tools", failures), ("paused missions", missions), ("goals", goals), ("promises", promises), ("predictions", calib)):
        source(name, fn)
    return out, failed


def describe(items: list[Opportunity], hidden: int = 0, failed: list[str] | None = None) -> str:
    if not items:
        text = "No opportunity with real evidence right now."
    else:
        text = "\n".join(f"{i}. [{o.kind}] {o.title}\n   evidence: {o.evidence}\n   benefit: {o.benefit}\n   complexity: {o.complexity}; risk: {o.risk}\n   next: {o.next_action}\n   id: {o.key}"
                         for i, o in enumerate(items, 1))
    if hidden:
        text += f"\n({hidden} dismissed item(s) not shown.)"
    if failed:
        text += "\nCould not check: " + ", ".join(failed) + "."
    return text
