"""Prediction ledger: Jarvis writes down what it expects BEFORE an action or check, then records what actually happened - and learns how far to trust itself.

The loop the world model was missing: predict -> act / observe -> compare -> record the error -> use the track record next time.
* ``predict`` stores the claim, the stated confidence (0..1), the topic (e.g. "restart fixes service", a tool name) and what would count as confirmation.
* ``resolve`` records the real outcome (right / wrong) with the evidence; a prediction never resolves itself, and an unresolved one never counts as right.
* ``calibration`` compares stated confidence with the real hit rate per confidence band, and gives a Brier score. With fewer than ``MIN_SAMPLES`` resolved
  predictions it says "not enough history" instead of a number.
* ``adjusted`` shrinks a new confidence toward the measured hit rate for that topic (never raises it above what was stated without evidence).
These are statistics about Jarvis's own guesses about your digital environment - not a model of physical reality, and not a guarantee.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

MIN_SAMPLES = 5
BANDS = ((0.0, 0.5, "below 50%"), (0.5, 0.75, "50-75%"), (0.75, 0.9, "75-90%"), (0.9, 1.01, "90%+"))


@dataclass
class Prediction:
    id: str
    topic: str
    claim: str
    confidence: float
    expect: str
    created: float
    outcome: str = ""          # "", "right", "wrong"
    evidence: str = ""
    resolved: float | None = None


class Ledger:
    def __init__(self, db: Any, clock: Callable[[], float] = time.time) -> None:
        self.db, self.clock = db, clock

    def predict(self, topic: str, claim: str, confidence: float, expect: str = "") -> Prediction:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        p = Prediction(uuid.uuid4().hex[:12], topic.strip().lower()[:60], claim.strip()[:300], float(confidence), expect.strip()[:200], self.clock())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO predictions (id, topic, claim, confidence, expect, created, outcome, evidence) VALUES (?,?,?,?,?,?,'','')",
                         (p.id, p.topic, p.claim, p.confidence, p.expect, p.created))
        return p

    def resolve(self, pid: str, right: bool, evidence: str) -> bool:
        if not evidence.strip():
            raise ValueError("an outcome needs the evidence it is based on")
        with self.db.transaction() as conn:
            return conn.execute("UPDATE predictions SET outcome = ?, evidence = ?, resolved = ? WHERE id = ? AND outcome = ''",
                                ("right" if right else "wrong", evidence.strip()[:300], self.clock(), pid)).rowcount > 0

    def items(self, open_only: bool = False, topic: str = "", limit: int = 50) -> list[Prediction]:
        q, a = "SELECT id, topic, claim, confidence, expect, created, outcome, evidence, resolved FROM predictions WHERE 1=1", []
        if open_only:
            q += " AND outcome = ''"
        if topic:
            q += " AND topic = ?"
            a.append(topic.strip().lower())
        with self.db.connect() as conn:
            return [Prediction(*r) for r in conn.execute(q + " ORDER BY created DESC LIMIT ?", (*a, limit))]


def calibration(items: list[Prediction]) -> dict[str, Any]:
    done = [p for p in items if p.outcome in ("right", "wrong")]
    if len(done) < MIN_SAMPLES:
        return {"enough": False, "resolved": len(done), "needed": MIN_SAMPLES}
    hits = sum(p.outcome == "right" for p in done)
    brier = sum((p.confidence - (1.0 if p.outcome == "right" else 0.0)) ** 2 for p in done) / len(done)
    bands = []
    for lo, hi, name in BANDS:
        grp = [p for p in done if lo <= p.confidence < hi]
        if grp:
            bands.append({"band": name, "n": len(grp), "stated": round(sum(p.confidence for p in grp) / len(grp), 2), "actual": round(sum(p.outcome == "right" for p in grp) / len(grp), 2)})
    stated = sum(p.confidence for p in done) / len(done)
    actual = hits / len(done)
    verdict = "overconfident" if stated - actual > 0.1 else "underconfident" if actual - stated > 0.1 else "about right"
    return {"enough": True, "resolved": len(done), "hit_rate": round(actual, 2), "stated": round(stated, 2), "brier": round(brier, 3), "verdict": verdict, "bands": bands}


def adjusted(confidence: float, topic_items: list[Prediction]) -> tuple[float, str]:
    """Shrink a stated confidence toward this topic's measured hit rate. No history -> unchanged, and said so."""
    done = [p for p in topic_items if p.outcome in ("right", "wrong")]
    if len(done) < MIN_SAMPLES:
        return confidence, f"no adjustment: only {len(done)} resolved prediction(s) on this topic"
    rate = sum(p.outcome == "right" for p in done) / len(done)
    w = min(len(done), 20) / 20                                   # more history = more weight, capped
    new = min(confidence, confidence * (1 - w) + rate * w) if rate < confidence else confidence
    return round(new, 2), f"past predictions on this topic were right {rate:.0%} of the time ({len(done)} resolved)"


def describe(cal: dict[str, Any]) -> str:
    if not cal["enough"]:
        return f"Not enough history to judge my predictions: {cal['resolved']} resolved, {cal['needed']} needed."
    lines = [f"Resolved predictions: {cal['resolved']}. I said {cal['stated']:.0%} on average and was right {cal['hit_rate']:.0%} - {cal['verdict']}. Brier score {cal['brier']} (0 is perfect, 0.25 is a coin flip)."]
    lines += [f"  when I said {b['band']}: right {b['actual']:.0%} of {b['n']}" for b in cal["bands"]]
    return "\n".join(lines)


# ---- automatic predictions around mission steps -------------------------------------------------------------------------
AUTO_PRIOR = 0.85
KEEP = 2000


def auto_predict(db: Any, tool: str, what: str = "") -> str:
    """Before a mission step: "this step will succeed", at the prior shrunk by this tool's own record. Returns the id ('' if it could not be stored)."""
    try:
        led = Ledger(db)
        topic = f"tool:{tool}"
        conf, _ = adjusted(AUTO_PRIOR, led.items(topic=topic, limit=200))
        return led.predict(topic, f"{tool} will succeed" + (f" ({what[:120]})" if what else ""), conf, "the step ends ok (and verified where the tool can check)").id
    except Exception:
        return ""


def auto_resolve(db: Any, pid: str, ok: bool, evidence: str) -> None:
    if not pid:
        return
    try:
        Ledger(db).resolve(pid, ok, evidence or ("the step ended ok" if ok else "the step failed"))
    except Exception:
        pass


def prune(db: Any, keep: int = KEEP) -> int:
    """Keep the newest ``keep`` predictions (the record is a statistic, not an archive)."""
    with db.transaction() as conn:
        return conn.execute("DELETE FROM predictions WHERE id NOT IN (SELECT id FROM predictions ORDER BY created DESC LIMIT ?)", (keep,)).rowcount
