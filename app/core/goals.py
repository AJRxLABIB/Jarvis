"""Goals: what you're working towards, with a deadline, a priority and tasks (they persist).

"My goal is to finish my project this month" -> a goal with a deadline at the end of the month. The AI may
propose the tasks ("set up the database, build the login, write tests"), you can add them by voice, and
progress follows the tasks ticked off (or is set directly when a goal has no tasks). The morning briefing
mentions goals that are due soon.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from app.tools.base import ToolError

if TYPE_CHECKING:
    from app.memory.database import Database

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | \
          {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_deadline(text: str | None, today: date) -> str | None:
    """'this month', 'this week', 'end of the year', 'by friday', 'in 2 weeks', '15 october', '2026-10-15'."""
    if not text or not text.strip():
        return None
    t = " ".join(text.lower().replace(",", " ").split()).removeprefix("by ").removeprefix("before ")
    t = t.removeprefix("the ").removeprefix("end of ").removeprefix("the ")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        try:
            return date.fromisoformat(t).isoformat()
        except ValueError as exc:
            raise ToolError(f"'{text}' is not a real date.", "bad_date") from exc
    if t in ("today", "tonight", "আজ"):
        return today.isoformat()
    if t in ("tomorrow", "কাল", "আগামীকাল"):
        return (today + timedelta(days=1)).isoformat()
    if t in ("this week", "the week", "week", "এই সপ্তাহে"):
        return (today + timedelta(days=6 - today.weekday())).isoformat()
    if t in ("next week",):
        return (today + timedelta(days=13 - today.weekday())).isoformat()
    if t in ("this month", "month", "the month", "এই মাসে"):
        return today.replace(day=calendar.monthrange(today.year, today.month)[1]).isoformat()
    if t in ("next month",):
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    if t in ("this year", "year", "the year", "এই বছর"):
        return date(today.year, 12, 31).isoformat()
    match = re.fullmatch(r"in (\d{1,3}|a|one|two|three|four) (day|week|month)s?", t)
    if match:
        count = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4}.get(match.group(1)) or int(match.group(1))
        days = {"day": 1, "week": 7, "month": 30}[match.group(2)] * count
        return (today + timedelta(days=days)).isoformat()
    day = t.removeprefix("next ").removeprefix("on ")
    if day in _WEEKDAYS:
        return (today + timedelta(days=(_WEEKDAYS.index(day) - today.weekday()) % 7 or 7)).isoformat()
    match = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? (?:of )?([a-z]+)(?: (\d{4}))?|([a-z]+) (\d{1,2})(?:st|nd|rd|th)?"
                         r"(?: (\d{4}))?", t)
    if match:
        number, name, year = (match.group(1), match.group(2), match.group(3)) if match.group(1) else \
            (match.group(5), match.group(4), match.group(6))
        month = _MONTHS.get(name)
        if month:
            try:
                found = date(int(year) if year else today.year, month, int(number))
            except ValueError as exc:
                raise ToolError(f"'{text}' is not a real date.", "bad_date") from exc
            if not year and found < today:
                found = found.replace(year=today.year + 1)
            return found.isoformat()
    raise ToolError(f"I didn't understand the deadline '{text}'. Say e.g. this month, by Friday, 15 October.",
                    "bad_date")


@dataclass
class Goal:
    id: int
    title: str
    why: str
    deadline: str | None
    priority: int
    status: str
    progress: int
    tasks: list[dict] = field(default_factory=list)

    def days_left(self, today: date) -> int | None:
        return (date.fromisoformat(self.deadline) - today).days if self.deadline else None

    def describe(self, today: date) -> str:
        parts = [f"{self.title}: {self.progress}%"]
        left = self.days_left(today)
        if left is not None:
            parts.append("overdue" if left < 0 else "due today" if left == 0 else f"{left} day(s) left")
        open_tasks = [t["text"] for t in self.tasks if not t["done"]]
        if open_tasks:
            parts.append(f"next: {open_tasks[0]}")
        return ", ".join(parts)


class GoalStore:
    def __init__(self, database: "Database", clock=lambda: datetime.now().astimezone()) -> None:
        self._db, self._clock = database, clock

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def add(self, title: str, deadline: str | None = None, priority: int = 2, why: str = "",
            tasks: list[str] | None = None) -> Goal:
        now = self._now()
        with self._db.transaction() as conn:
            goal_id = conn.execute("INSERT INTO goals (title, why, deadline, priority, created_at, updated_at) "
                                   "VALUES (?, ?, ?, ?, ?, ?)", (title, why, deadline, priority, now, now)).lastrowid
            for position, text in enumerate(tasks or [], start=1):
                conn.execute("INSERT INTO goal_tasks (goal_id, position, text) VALUES (?, ?, ?)",
                             (goal_id, position, text))
        return self.get(goal_id)

    def get(self, goal_id: int) -> Goal:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
            if row is None:
                raise ToolError("There's no such goal.", "not_found")
            tasks = [dict(r) for r in conn.execute("SELECT id, position, text, done, depends_on FROM goal_tasks "
                                                   "WHERE goal_id = ? ORDER BY position", (goal_id,))]
        return Goal(row["id"], row["title"], row["why"], row["deadline"], row["priority"], row["status"],
                    row["progress"], tasks)

    def active(self) -> list[Goal]:
        with self._db.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM goals WHERE status = 'active' "
                                              "ORDER BY deadline IS NULL, deadline, priority, id")]
        return [self.get(i) for i in ids]

    def find(self, name: str | None) -> Goal:
        goals = self.active()
        if not goals:
            raise ToolError("You have no active goals. Say e.g. 'my goal is to finish my project this month'.",
                            "no_goals")
        if not name or not name.strip():
            if len(goals) == 1:
                return goals[0]
            raise ToolError("Which goal? " + "; ".join(g.title for g in goals[:5]) + ".", "ambiguous")
        key = name.lower().strip()
        if key.isdigit() and 1 <= int(key) <= len(goals):
            return goals[int(key) - 1]
        words = set(re.findall(r"\w{3,}", key))
        scored = sorted(goals, key=lambda g: -len(words & set(re.findall(r"\w{3,}", g.title.lower()))))
        if words & set(re.findall(r"\w{3,}", scored[0].title.lower())):
            return scored[0]
        raise ToolError(f"I don't see a goal about '{name}'. Your goals: " + "; ".join(g.title for g in goals[:5]),
                        "not_found")

    def add_task(self, goal: Goal, text: str) -> Goal:
        with self._db.transaction() as conn:
            position = (conn.execute("SELECT MAX(position) FROM goal_tasks WHERE goal_id = ?", (goal.id,))
                        .fetchone()[0] or 0) + 1
            conn.execute("INSERT INTO goal_tasks (goal_id, position, text) VALUES (?, ?, ?)", (goal.id, position, text))
        return self._recompute(goal.id)

    def complete_task(self, goal: Goal, which: str) -> tuple[Goal, str]:
        open_tasks = [t for t in goal.tasks if not t["done"]]
        if not open_tasks:
            raise ToolError(f"'{goal.title}' has no open tasks.", "no_tasks")
        key = which.lower().strip()
        task = None
        if key.isdigit():
            task = next((t for t in goal.tasks if t["position"] == int(key)), None)
        if task is None:
            words = set(re.findall(r"\w{3,}", key))
            task = max(open_tasks, key=lambda t: len(words & set(re.findall(r"\w{3,}", t["text"].lower()))))
            if not words & set(re.findall(r"\w{3,}", task["text"].lower())):
                raise ToolError("Which task? Open: " + "; ".join(f"{t['position']}. {t['text']}" for t in open_tasks),
                                "ambiguous")
        with self._db.transaction() as conn:
            conn.execute("UPDATE goal_tasks SET done = 1, done_at = ? WHERE id = ?", (self._now(), task["id"]))
        return self._recompute(goal.id), task["text"]

    def set_progress(self, goal: Goal, progress: int) -> Goal:
        with self._db.transaction() as conn:
            conn.execute("UPDATE goals SET progress = ?, updated_at = ? WHERE id = ?",
                         (max(0, min(100, progress)), self._now(), goal.id))
        return self.get(goal.id)

    def set_status(self, goal: Goal, status: str) -> Goal:
        with self._db.transaction() as conn:
            conn.execute("UPDATE goals SET status = ?, updated_at = ?, completed_at = ?, progress = "
                         "CASE WHEN ? = 'done' THEN 100 ELSE progress END WHERE id = ?",
                         (status, self._now(), self._now() if status == "done" else None, status, goal.id))
        return self.get(goal.id)

    def _recompute(self, goal_id: int) -> Goal:
        goal = self.get(goal_id)
        if goal.tasks:
            progress = round(100 * sum(1 for t in goal.tasks if t["done"]) / len(goal.tasks))
            with self._db.transaction() as conn:
                conn.execute("UPDATE goals SET progress = ?, updated_at = ? WHERE id = ?", (progress, self._now(), goal_id))
            goal = self.get(goal_id)
        return goal

    def briefing_line(self, today: date, within_days: int = 7) -> str:
        soon = [g for g in self.active() if (g.days_left(today) is not None and g.days_left(today) <= within_days)]
        if not soon:
            return ""
        return "Goals due soon: " + "; ".join(g.describe(today) for g in soon[:3]) + "."
