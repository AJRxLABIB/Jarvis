"""Technical experience memory: what was wrong, what was tried, what failed, what worked, how it was verified - and whether it applies again.

A record is written from facts you (or a verified tool result) give: problem, environment (name=value: os, app, version...), error text, attempts that
FAILED, the fix that worked, and the verification. ``recall`` finds similar past problems by wording and error text, then compares the environment:
* same environment keys match                  -> "applies" (still offered as a suggestion to try, never run automatically);
* a version / environment value differs        -> "may not apply", with exactly what differs;
* the record has no verification               -> "unverified fix" (ranked below verified ones);
* older than ``STALE_DAYS``                    -> flagged as old.
Failed attempts are shown too, so the same dead end is not tried twice. Nothing here executes anything; records can be listed and deleted.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

STALE_DAYS = 365
_STOP = {"the", "a", "an", "is", "to", "of", "in", "on", "and", "not", "it", "my", "i", "for", "with", "when", "error", "failed"}


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_\.\-]{2,}", (text or "").lower()) if w not in _STOP}


@dataclass
class Experience:
    id: str
    problem: str
    error: str
    environment: dict[str, str]
    failed: list[str]
    solution: str
    verification: str
    created: float
    source: str = "user"


@dataclass
class Match:
    exp: Experience
    score: float
    verdict: str                 # applies | may not apply | unverified fix
    differs: list[str] = field(default_factory=list)
    old: bool = False


def parse_env(text: str) -> dict[str, str]:
    out = {}
    for part in re.split(r"[,;\n]", text or ""):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip().lower()[:30]] = v.strip()[:60]
    return out


class Store:
    def __init__(self, db: Any, clock: Callable[[], float] = time.time) -> None:
        self.db, self.clock = db, clock

    def add(self, problem: str, solution: str, error: str = "", environment: dict[str, str] | None = None, failed: list[str] | None = None,
            verification: str = "", source: str = "user") -> Experience:
        if not problem.strip() or not solution.strip():
            raise ValueError("an experience needs the problem and what solved it")
        e = Experience(uuid.uuid4().hex[:12], problem.strip()[:300], error.strip()[:500], environment or {}, [f.strip()[:200] for f in (failed or []) if f.strip()][:10],
                       solution.strip()[:500], verification.strip()[:300], self.clock(), source[:20])
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO tech_experiences (id, problem, error, environment, failed, solution, verification, created, source) VALUES (?,?,?,?,?,?,?,?,?)",
                         (e.id, e.problem, e.error, json.dumps(e.environment), json.dumps(e.failed), e.solution, e.verification, e.created, e.source))
        return e

    def all(self, limit: int = 200) -> list[Experience]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id, problem, error, environment, failed, solution, verification, created, source FROM tech_experiences ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [Experience(r[0], r[1], r[2], json.loads(r[3]), json.loads(r[4]), r[5], r[6], r[7], r[8]) for r in rows]

    def delete(self, eid: str) -> bool:
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM tech_experiences WHERE id = ?", (eid,)).rowcount > 0

    def recall(self, problem: str, error: str = "", environment: dict[str, str] | None = None, limit: int = 3) -> list[Match]:
        return recall(self.all(), problem, error, environment or {}, self.clock(), limit)


def recall(items: list[Experience], problem: str, error: str, environment: dict[str, str], now: float, limit: int = 3) -> list[Match]:
    q, qe = words(problem), words(error)
    out = []
    for e in items:
        pw, ew = words(e.problem), words(e.error)
        s = (len(q & pw) / max(1, len(q | pw))) + (1.5 * len(qe & ew) / max(1, len(qe | ew)) if qe and ew else 0.0)
        if s < 0.2:
            continue
        differs = [f"{k}: then {v}, now {environment[k]}" for k, v in e.environment.items() if k in environment and environment[k].lower() != v.lower()]
        verdict = "may not apply" if differs else ("unverified fix" if not e.verification else "applies")
        rank = s - (0.3 if differs else 0) - (0.15 if not e.verification else 0)
        out.append(Match(e, round(rank, 3), verdict, differs, (now - e.created) > STALE_DAYS * 86400))
    return sorted(out, key=lambda m: -m.score)[:limit]


def describe(matches: list[Match]) -> str:
    if not matches:
        return "No similar past problem is recorded. I have nothing to reuse here."
    lines = []
    for m in matches:
        e = m.exp
        head = {"applies": "Same environment - worth trying", "may not apply": "MAY NOT APPLY", "unverified fix": "Fix was never verified"}[m.verdict]
        lines.append(f"[{head}] {e.problem}\n  worked: {e.solution}" + (f"\n  verified by: {e.verification}" if e.verification else "")
                     + (f"\n  did NOT work: {'; '.join(e.failed)}" if e.failed else "") + (f"\n  differs: {'; '.join(m.differs)}" if m.differs else "")
                     + ("\n  note: more than a year old" if m.old else "") + f"\n  id {e.id}")
    return "\n".join(lines) + "\nThese are past cases to consider, not instructions - nothing was run."


# ---- candidates from the action log (never saved automatically) ------------------------------------------------------------
_SECRET = ("password", "token", "secret", "key", "passwd", "credential")
WINDOW_S = 900


def _safe_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in list((args or {}).items())[:6]:
        parts.append(f"{k}=<hidden>" if any(s in str(k).lower() for s in _SECRET) else f"{k}={str(v)[:40]!r}")
    return ", ".join(parts) or "no arguments"


def load_audit(db: Any, limit: int = 3000) -> list[dict[str, Any]]:
    """Tool results from the audit log, oldest first: {ts (epoch), tool, ok, error, message, arguments}."""
    from datetime import datetime

    out = []
    with db.connect() as conn:
        rows = conn.execute("SELECT ts, tool, details FROM audit_log WHERE event = 'tool_result' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    for ts, tool, details in reversed(rows):
        try:
            d = json.loads(details or "{}")
            out.append({"ts": datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp(), "tool": tool, "ok": bool(d.get("ok")), "error": str(d.get("error") or ""),
                        "message": str(d.get("message") or ""), "arguments": d.get("arguments") or {}})
        except (ValueError, TypeError):
            continue
    return out


def mine(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A tool call that failed and then SUCCEEDED with different arguments within 15 minutes = a candidate "what fixed it". Candidates only: you decide."""
    seen, out = set(), []
    for i, r in enumerate(rows):
        if r["ok"] or not r["tool"]:
            continue
        for later in rows[i + 1:]:
            if later["ts"] - r["ts"] > WINDOW_S:
                break
            if later["tool"] == r["tool"] and later["ok"] and later["arguments"] != r["arguments"]:
                key = (r["tool"], r["error"])
                if key not in seen:
                    seen.add(key)
                    out.append({"problem": f"{r['tool']} failed: {r['message'][:120] or r['error']}", "error": r["error"], "failed": [f"{r['tool']}({_safe_args(r['arguments'])})"],
                                "solution": f"{r['tool']} worked with {_safe_args(later['arguments'])}", "verification": "the later call succeeded (from the action log)"})
                break
    return out[:20]
