"""Deadline risk: what is overdue, due soon, blocked or too much - with the rule behind every line. Reads the existing todos and goals; invents nothing.

Rules (transparent, no scoring magic):
* OVERDUE      - an open todo whose due date has passed; an active goal whose deadline has passed and is not done;
* DUE SOON     - due within 48 h (todos) / 7 days (goals);
* BEHIND PACE  - a goal with a start (created_at) and deadline whose progress is more than 15 points below the straight-line share of time elapsed;
* BLOCKED      - a goal task that depends on a task that is not done;
* OVERLOADED   - more than ``MAX_PER_DAY`` todos due on the same day.
Not measured (and said so): repeated postponement (nothing records it), calendar commitments (no calendar is connected), effort estimates.
Pure ``assess`` over plain rows; ``load`` reads the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

SOON_TODO_H = 48
SOON_GOAL_D = 7
PACE_GAP = 15
MAX_PER_DAY = 6


@dataclass
class Risk:
    level: str            # high | medium | low
    kind: str
    text: str
    rule: str


def _day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def assess(todos: list[dict], goals: list[dict], today: date) -> list[Risk]:
    risks: list[Risk] = []
    per_day: dict[date, int] = {}
    for t in todos:
        d = _day(t.get("due"))
        if t.get("done") or d is None:
            continue
        per_day[d] = per_day.get(d, 0) + 1
        if d < today:
            risks.append(Risk("high", "overdue", f"'{t['text']}' was due {(today - d).days} day(s) ago", "open todo with a due date in the past"))
        elif (d - today).days * 24 <= SOON_TODO_H:
            risks.append(Risk("medium", "due_soon", f"'{t['text']}' is due {'today' if d == today else 'tomorrow' if (d - today).days == 1 else d.isoformat()}", "open todo due within 48 hours"))
    for d, n in sorted(per_day.items()):
        if n > MAX_PER_DAY and d >= today:
            risks.append(Risk("medium", "overloaded", f"{n} todos are due on {d.isoformat()}", f"more than {MAX_PER_DAY} todos on one day"))
    for g in goals:
        if g.get("status", "active") != "active":
            continue
        dl = _day(g.get("deadline"))
        if dl is not None:
            if dl < today:
                risks.append(Risk("high", "overdue", f"goal '{g['title']}' passed its deadline ({dl.isoformat()}) at {g.get('progress', 0)}%", "active goal past its deadline"))
            elif (dl - today).days <= SOON_GOAL_D:
                risks.append(Risk("medium", "due_soon", f"goal '{g['title']}' is due in {(dl - today).days} day(s) at {g.get('progress', 0)}%", "active goal due within 7 days"))
            start = _day(g.get("created_at"))
            if start and dl > start and start <= today <= dl:
                expected = 100 * (today - start).days / (dl - start).days
                if expected - float(g.get("progress", 0)) > PACE_GAP:
                    risks.append(Risk("medium", "behind_pace", f"goal '{g['title']}' is at {g.get('progress', 0)}% but {expected:.0f}% of its time has passed",
                                      f"progress more than {PACE_GAP} points below straight-line pace"))
        done = {t["id"] for t in g.get("tasks", []) if t.get("done")}
        for t in g.get("tasks", []):
            dep = t.get("depends_on")
            if not t.get("done") and dep and dep not in done:
                risks.append(Risk("low", "blocked", f"'{t['text']}' (goal '{g['title']}') waits for an unfinished task", "depends on a task that is not done"))
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(risks, key=lambda r: order[r.level])


def load(db: Any) -> tuple[list[dict], list[dict]]:
    with db.connect() as conn:
        todos = [dict(r) for r in conn.execute("SELECT id, text, due, done FROM todos WHERE done = 0")]
        goals = [dict(r) for r in conn.execute("SELECT id, title, deadline, progress, status, created_at FROM goals WHERE status = 'active'")]
        for g in goals:
            g["tasks"] = [dict(r) for r in conn.execute("SELECT id, text, done, depends_on FROM goal_tasks WHERE goal_id = ?", (g["id"],))]
    return todos, goals


def describe(risks: list[Risk]) -> str:
    if not risks:
        return "Nothing is overdue, due soon, blocked or overloaded in your todos and goals. (Not measured: repeated postponement, calendar commitments, effort estimates.)"
    lines = [f"[{r.level.upper()}] {r.text} (rule: {r.rule})" for r in risks[:15]]
    return "\n".join(lines) + "\nNot measured: repeated postponement, calendar commitments, effort estimates."
