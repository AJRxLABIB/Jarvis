"""Decision records and option comparison.

A decision record keeps what you chose, what else was on the table, the criteria and the evidence, and later how it
turned out. It is a note you can correct or delete, never a rule: ``relevant`` only SHOWS earlier decisions about a
topic (with their age and status) and nothing consults them automatically.

``compare`` is the counterfactual helper. Every number carries its basis - measured (a real benchmark), simulated
(a model of known state) or estimated (a judgement). A criterion with a missing number for any option is left out of the
ranking and reported (an unknown is never treated as zero); with nothing comparable there is NO ranking; a margin under
10% is "too close to call"; and when the evidence is only simulated or estimated, a reversible option is suggested first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.tools.base import ToolError

if TYPE_CHECKING:
    from app.memory.database import Database

BASES = ("measured", "simulated", "estimated")         # strongest first
CLOSE = 0.10
_WORD = re.compile(r"[a-z0-9][a-z0-9.+#_-]{1,}")
_STOP = {"the", "and", "for", "use", "using", "with", "our", "my", "a", "an", "to", "of", "in", "on"}


# ------------------------------------------------------------------ comparison


@dataclass(frozen=True)
class Score:
    value: float | None
    basis: str = "estimated"            # measured | simulated | estimated
    note: str = ""


@dataclass
class Option:
    name: str
    scores: dict[str, Score] = field(default_factory=dict)
    reversible: bool = True


@dataclass
class Comparison:
    ranking: list[tuple[str, float]]    # best first, 0..1
    used: list[str]                     # criteria that took part
    left_out: list[str]                 # criteria with an unknown for some option
    basis: str                          # the WEAKEST basis among the used scores
    verdict: str                        # ranked | too_close | no_evidence
    notes: list[str]

    def describe(self) -> str:
        if self.verdict == "no_evidence":
            return "I can't rank these: no criterion has a number for every option. " + " ".join(self.notes)
        lines = [("Too close to call: " if self.verdict == "too_close" else "Best fit: ") +
                 ", ".join(f"{n} ({v:.2f})" for n, v in self.ranking) + f"   [basis: {self.basis}]"]
        lines += self.notes
        return "\n".join(lines)


def compare(options: list[Option], weights: dict[str, float] | None = None,
            higher_is_better: dict[str, bool] | None = None) -> Comparison:
    if len(options) < 2:
        raise ToolError("Give at least two options to compare.", "invalid")
    weights, higher = weights or {}, higher_is_better or {}
    criteria = sorted({c for o in options for c in o.scores})
    used, left_out, notes = [], [], []
    for c in criteria:
        if all(c in o.scores and o.scores[c].value is not None for o in options):
            used.append(c)
        else:
            left_out.append(c)
    if left_out:
        notes.append("Left out (unknown for at least one option): " + ", ".join(left_out) + ".")
    if not used:
        return Comparison([], [], left_out, "estimated", "no_evidence", notes)
    totals = {o.name: 0.0 for o in options}
    total_weight = 0.0
    for c in used:
        values = [o.scores[c].value for o in options]
        lo, hi = min(values), max(values)           # type: ignore[type-var]
        weight = max(0.0, float(weights.get(c, 1.0)))
        total_weight += weight
        for o in options:
            v = o.scores[c].value
            norm = 1.0 if hi == lo else (v - lo) / (hi - lo)          # type: ignore[operator]
            totals[o.name] += weight * (norm if higher.get(c, True) else 1.0 - norm)
    if total_weight <= 0:
        return Comparison([], used, left_out, "estimated", "no_evidence", notes + ["All weights are zero."])
    ranking = sorted(((n, round(t / total_weight, 3)) for n, t in totals.items()), key=lambda kv: -kv[1])
    basis = max((o.scores[c].basis for o in options for c in used), key=lambda b: BASES.index(b) if b in BASES else 2)
    close = ranking[0][1] - ranking[1][1] < CLOSE
    if basis != "measured":
        best = next(o for o in options if o.name == ranking[0][0])
        safer = [o.name for o in options if o.reversible]
        if not best.reversible and safer:
            notes.append(f"The evidence is {basis}, not measured, and {best.name} can't be undone: try {safer[0]} first.")
        else:
            notes.append(f"The numbers are {basis}, not measured: treat this as a guide.")
    return Comparison(ranking, used, left_out, basis, "too_close" if close else "ranked", notes)


# ------------------------------------------------------------------ records


@dataclass
class DecisionRecord:
    id: int
    title: str
    chosen: str
    alternatives: list[str]
    criteria: list[str]
    evidence: list[str]
    outcome: str
    status: str
    project: str
    created_at: str
    updated_at: str

    def describe(self) -> str:
        parts = [f"#{self.id} {self.title}: chose {self.chosen}"]
        if self.alternatives:
            parts.append("over " + ", ".join(self.alternatives))
        if self.criteria:
            parts.append("because " + "; ".join(self.criteria))
        if self.outcome:
            parts.append(f"outcome: {self.outcome}")
        if self.status != "active":
            parts.append(f"[{self.status}]")
        return " - ".join(parts)


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


class DecisionStore:
    def __init__(self, database: "Database", clock=lambda: datetime.now(timezone.utc)) -> None:
        self._db, self._clock = database, clock

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    @staticmethod
    def _row(row: Any) -> DecisionRecord:
        return DecisionRecord(row["id"], row["title"], row["chosen"], json.loads(row["alternatives"]),
                              json.loads(row["criteria"]), json.loads(row["evidence"]), row["outcome"], row["status"],
                              row["project"], row["created_at"], row["updated_at"])

    def record(self, title: str, chosen: str, alternatives: list[str] | None = None, criteria: list[str] | None = None,
               evidence: list[str] | None = None, project: str = "") -> DecisionRecord:
        title, chosen = " ".join(title.split()), " ".join(chosen.split())
        if not title or not chosen:
            raise ToolError("A decision needs a title and what was chosen.", "invalid")
        now = self._now()
        with self._db.transaction() as conn:
            new_id = conn.execute(
                "INSERT INTO decision_records (title, chosen, alternatives, criteria, evidence, project, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (title[:200], chosen[:300], json.dumps(alternatives or [], ensure_ascii=False),
                 json.dumps(criteria or [], ensure_ascii=False), json.dumps(evidence or [], ensure_ascii=False),
                 project[:100], now, now)).lastrowid
        return self.get(int(new_id))

    def get(self, decision_id: int) -> DecisionRecord:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM decision_records WHERE id = ?", (decision_id,)).fetchone()
        if row is None:
            raise ToolError("There's no such decision.", "not_found")
        return self._row(row)

    def list(self, project: str = "", include_inactive: bool = False, limit: int = 30) -> list[DecisionRecord]:
        sql, args = "SELECT * FROM decision_records WHERE 1=1", []
        if project:
            sql += " AND lower(project) = ?"
            args.append(project.lower())
        if not include_inactive:
            sql += " AND status = 'active'"
        with self._db.connect() as conn:
            rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._row(r) for r in rows]

    def correct(self, decision_id: int, **fields: Any) -> DecisionRecord:
        """Change what was recorded. The old value is kept in the evidence list so a correction is visible, not silent."""
        record = self.get(decision_id)
        allowed = {"title", "chosen", "alternatives", "criteria", "project"}
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not changes:
            raise ToolError("Nothing to change.", "invalid")
        trail = list(record.evidence)
        for key in changes:
            trail.append(f"corrected {key} (was: {getattr(record, key)!r})")
        with self._db.transaction() as conn:
            for key, value in changes.items():
                stored = json.dumps(value, ensure_ascii=False) if key in ("alternatives", "criteria") else str(value)
                conn.execute(f"UPDATE decision_records SET {key} = ? WHERE id = ?", (stored, decision_id))
            conn.execute("UPDATE decision_records SET evidence = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(trail, ensure_ascii=False), self._now(), decision_id))
        return self.get(decision_id)

    def set_outcome(self, decision_id: int, outcome: str, status: str | None = None) -> DecisionRecord:
        self.get(decision_id)
        if status is not None and status not in ("active", "superseded", "reversed"):
            raise ToolError("Status must be active, superseded or reversed.", "invalid")
        with self._db.transaction() as conn:
            conn.execute("UPDATE decision_records SET outcome = ?, status = COALESCE(?, status), updated_at = ? WHERE id = ?",
                         (outcome[:300], status, self._now(), decision_id))
        return self.get(decision_id)

    def delete(self, decision_id: int) -> None:
        self.get(decision_id)
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM decision_records WHERE id = ?", (decision_id,))

    def relevant(self, topic: str, limit: int = 5) -> list[tuple[DecisionRecord, int]]:
        """Earlier decisions that share words with ``topic`` (with their age in days). Context to show, never a rule."""
        wanted = _words(topic)
        if not wanted:
            return []
        now = self._clock()
        found = []
        for record in self.list(include_inactive=True, limit=300):
            if wanted & _words(f"{record.title} {record.chosen} {record.project}"):
                try:
                    age = (now - datetime.fromisoformat(record.created_at)).days
                except ValueError:
                    age = 0
                found.append((record, age))
        return found[:limit]
