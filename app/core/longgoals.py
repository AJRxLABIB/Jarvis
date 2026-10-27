"""Long-horizon goals: progress from verified results, stalls, realistic plans and reviews - read-only, built on the
existing goals (``app/core/goals.py``) and their tasks.

* Progress counts ticked-off tasks (the results), never the number of tasks created. A goal with no tasks has no
  verifiable progress and says so.
* Pace comes from your own history (tasks actually completed per day). With fewer than ``MIN_HISTORY`` completions there
  is no pace and no forecast - it never invents one. Availability you give is only an UPPER bound on the pace, because a
  free calendar slot is not productive time.
* The forecast is a range, labelled an estimate; "unrealistic" is said only when even the optimistic end misses the date.
* ``replan`` PROPOSES changes (move the date, defer tasks) with what changes and why. Nothing is edited, dropped or
  re-prioritised by this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.memory.database import Database

MIN_HISTORY = 3          # completed tasks needed before a pace means anything
STALL_DAYS = 14
OPTIMISTIC, PESSIMISTIC = 1.25, 0.6
HISTORY_DAYS = 60


@dataclass
class Assessment:
    goal_id: int
    title: str
    done: int
    total: int
    status: str                              # no_tasks | on_track | tight | unrealistic | no_pace | no_deadline | finished | stalled
    stalled: bool = False
    blocked: list[str] = field(default_factory=list)
    pace_per_day: float | None = None
    forecast: tuple[date, date] | None = None       # (earliest, latest) rough finish dates
    deadline: date | None = None
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        head = f"{self.title}: {self.done}/{self.total} tasks done" if self.total else f"{self.title}: no tasks, so progress can't be verified"
        parts = [head, f"[{self.status.replace('_', ' ')}]"]
        if self.forecast:
            parts.append(f"rough finish {self.forecast[0].isoformat()} to {self.forecast[1].isoformat()} (an estimate)")
        if self.deadline:
            parts.append(f"due {self.deadline.isoformat()}")
        return " ".join(parts) + "".join("\n  - " + n for n in self.notes)


def pace(db: "Database", today: date, goal_id: int | None = None) -> tuple[float | None, int]:
    """(tasks completed per day over the last HISTORY_DAYS, completions seen). None when there is too little history."""
    since = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    sql, args = "SELECT done_at FROM goal_tasks WHERE done = 1 AND done_at >= ?", [since]
    if goal_id is not None:
        sql += " AND goal_id = ?"
        args.append(goal_id)
    with db.connect() as conn:
        stamps = [r[0] for r in conn.execute(sql, args) if r[0]]
    if len(stamps) < MIN_HISTORY:
        return None, len(stamps)
    first = min(datetime.fromisoformat(s[:19]).date() for s in stamps)
    span = max(7, (today - first).days + 1)                  # never divide by a tiny window
    return len(stamps) / span, len(stamps)


def _tasks(db: "Database", goal_id: int) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id, position, text, done, done_at, depends_on FROM goal_tasks "
                                              "WHERE goal_id = ? ORDER BY position", (goal_id,))]


def assess(db: "Database", goal: Any, today: date, hours_per_week: float | None = None,
           minutes_per_task: float | None = None) -> Assessment:
    tasks = _tasks(db, goal.id)
    done = [t for t in tasks if t["done"]]
    open_tasks = [t for t in tasks if not t["done"]]
    deadline = date.fromisoformat(goal.deadline) if goal.deadline else None
    a = Assessment(goal.id, goal.title, len(done), len(tasks), "no_tasks", deadline=deadline)
    if not tasks:
        a.notes.append("Add tasks (the results you can tick off) so progress and pace can be measured.")
        return a
    if not open_tasks:
        a.status = "finished"
        return a
    open_ids = {t["id"] for t in open_tasks}
    a.blocked = [t["text"] for t in open_tasks if t["depends_on"] and t["depends_on"] in open_ids]
    if a.blocked:
        a.notes.append(f"{len(a.blocked)} task(s) wait for an unfinished task: " + "; ".join(a.blocked[:3]))
    last_done = max((datetime.fromisoformat(t["done_at"][:19]).date() for t in done if t["done_at"]), default=None)
    with db.connect() as conn:
        row = conn.execute("SELECT created_at FROM goals WHERE id = ?", (goal.id,)).fetchone()
    started = datetime.fromisoformat(row[0][:19]).date() if row else today
    quiet_since = last_done or started
    if (today - quiet_since).days >= STALL_DAYS:
        a.stalled = True
        a.notes.append(f"No task finished in {(today - quiet_since).days} days.")
    rate, seen = pace(db, today, None)            # your overall pace: one goal's own history is usually too thin
    if rate is not None and hours_per_week and minutes_per_task:
        capacity = hours_per_week * 60 / minutes_per_task / 7
        if capacity < rate:
            a.notes.append(f"You said about {hours_per_week:g} h/week, which caps the pace below your past rate.")
        rate = min(rate, capacity)
    a.pace_per_day = round(rate, 3) if rate is not None else None
    if rate is None or rate <= 0:
        a.status = "stalled" if a.stalled else "no_pace"
        a.notes.append(f"Only {seen} completed task(s) in the last {HISTORY_DAYS} days: not enough history to estimate a pace.")
        return a
    remaining = len(open_tasks)
    a.forecast = (today + timedelta(days=math.ceil(remaining / (rate * OPTIMISTIC))),
                  today + timedelta(days=math.ceil(remaining / (rate * PESSIMISTIC))))
    if deadline is None:
        a.status = "stalled" if a.stalled else "no_deadline"
    elif a.forecast[0] > deadline:
        a.status = "unrealistic"
        a.notes.append("Even at your best recent pace the remaining tasks end after the deadline.")
    elif a.forecast[1] <= deadline:
        a.status = "on_track"
    else:
        a.status = "tight"
        a.notes.append("It only fits if your pace holds; a slower stretch would miss the date.")
    if a.stalled and a.status in ("on_track", "tight"):
        a.status = "stalled"
    return a


def replan(a: Assessment, goal: Any, db: "Database", today: date | None = None) -> list[str]:
    """Proposals only. Each says what would change and why; applying any of them is your decision."""
    if a.status not in ("unrealistic", "tight", "stalled", "no_pace"):
        return []
    out: list[str] = []
    open_tasks = [t for t in _tasks(db, goal.id) if not t["done"]]
    if a.status == "stalled":
        out.append(f"'{a.title}' has not moved. Pick the single next task and schedule it, or pause the goal (nothing changes until you say).")
    if a.forecast and a.deadline and a.status in ("unrealistic", "tight"):
        out.append(f"Move the deadline from {a.deadline.isoformat()} to about {a.forecast[1].isoformat()} (the cautious end of the estimate).")
        needed = max(1, len(open_tasks) - int(((a.deadline - (today or date.today())).days) * (a.pace_per_day or 0) * OPTIMISTIC))
        if needed < len(open_tasks):
            tail = [t["text"] for t in open_tasks[-needed:]]
            out.append(f"Or defer {needed} task(s) to fit the date: " + "; ".join(tail) + ".")
    if a.blocked:
        out.append("Unblock first: " + a.blocked[0] + ".")
    if a.status == "no_pace":
        out.append("Complete a few tasks first (or tell me the hours you can give per week) so a real estimate becomes possible.")
    return out


def assess_all(db: "Database", goals: list[Any], today: date, **kw: Any) -> list[Assessment]:
    return [assess(db, g, today, **kw) for g in goals]


def weekly_review(db: "Database", goals: list[Any], today: date, enabled: bool = True) -> str:
    """What actually happened this week, from task completions. Private reflections are not collected: only goals and
    tasks you already stored. ``enabled=False`` returns nothing."""
    if not enabled:
        return ""
    since = (today - timedelta(days=7)).isoformat()
    with db.connect() as conn:
        finished = [dict(r) for r in conn.execute(
            "SELECT t.text, g.title FROM goal_tasks t JOIN goals g ON g.id = t.goal_id WHERE t.done = 1 AND t.done_at >= ? "
            "ORDER BY t.done_at", (since,))]
    lines = [f"Week to {today.isoformat()}"]
    lines.append("Finished: " + ("; ".join(f"{f['text']} ({f['title']})" for f in finished[:8]) if finished else "no tasks were ticked off."))
    stalled, due_soon, finished_goals = [], [], []
    for a in assess_all(db, goals, today):
        if a.status == "finished":
            finished_goals.append(a.title)
        if a.stalled:
            stalled.append(a.title)
        if a.deadline and 0 <= (a.deadline - today).days <= 14:
            due_soon.append(f"{a.title} ({(a.deadline - today).days} d, {a.status.replace('_', ' ')})")
    if finished_goals:
        lines.append("Goals with every task done: " + ", ".join(finished_goals))
    if due_soon:
        lines.append("Due within two weeks: " + "; ".join(due_soon))
    if stalled:
        lines.append("Not moving (no task in two weeks): " + ", ".join(stalled))
    open_total = sum(max(0, a.total - a.done) for a in assess_all(db, goals, today))
    lines.append(f"Open tasks across goals: {open_total}.")
    lines.append("Lessons and recurring obstacles are not guessed here: add them to a decision record or a note.")
    return "\n".join(lines)
