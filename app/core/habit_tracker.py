"""Personal habit tracker: habits you tick off each day ("drink water", "exercise", "read 20 pages").

Streak = consecutive days done, ending today (or yesterday, so a streak isn't "broken" before the day is over).
Local only. Not to be confused with app/agent/habits.py, which learns which apps you open when.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip().strip(".!?")).lower()[:60]
    if not name:
        raise ValueError("Which habit?")
    return name


@dataclass
class Habit:
    id: int
    name: str
    done_today: bool
    streak: int
    best: int
    last_7: int          # days done in the last 7 (including today)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "done_today": self.done_today, "streak": self.streak,
                "best": self.best, "last_7": self.last_7}


def streaks(days: set[date], today: date) -> tuple[int, int]:
    """(current, best). Current counts back from today, or from yesterday if today isn't done yet."""
    best = run = 0
    previous = None
    for d in sorted(days):
        run = run + 1 if previous is not None and d - previous == timedelta(days=1) else 1
        best = max(best, run)
        previous = d
    cursor = today if today in days else today - timedelta(days=1)
    current = 0
    while cursor in days:
        current += 1
        cursor -= timedelta(days=1)
    return current, best


class HabitTracker:
    def __init__(self, database) -> None:
        self.db = database

    def _id(self, conn, name: str) -> int | None:
        row = conn.execute("SELECT id FROM habit_defs WHERE name = ? AND archived = 0", (clean_name(name),)).fetchone()
        return None if row is None else int(row["id"])

    def add(self, name: str) -> bool:
        """True if it is new (a removed habit with the same name comes back with its history)."""
        name = clean_name(name)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT id, archived FROM habit_defs WHERE name = ?", (name,)).fetchone()
            if row is not None:
                conn.execute("UPDATE habit_defs SET archived = 0 WHERE id = ?", (row["id"],))
                return bool(row["archived"])
            conn.execute("INSERT INTO habit_defs (name, created) VALUES (?, ?)",
                         (name, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            return True

    def remove(self, name: str) -> bool:
        with self.db.transaction() as conn:
            return conn.execute("UPDATE habit_defs SET archived = 1 WHERE name = ? AND archived = 0",
                                (clean_name(name),)).rowcount > 0

    def check(self, name: str, day: date, done: bool = True) -> bool:
        """Tick (or untick) a habit for a day. Unknown habit -> ValueError. True if something changed."""
        with self.db.transaction() as conn:
            habit_id = self._id(conn, name)
            if habit_id is None:
                raise ValueError(f"There is no habit called '{clean_name(name)}'.")
            if done:
                return conn.execute("INSERT OR IGNORE INTO habit_checks (habit_id, day) VALUES (?, ?)",
                                    (habit_id, day.isoformat())).rowcount > 0
            return conn.execute("DELETE FROM habit_checks WHERE habit_id = ? AND day = ?",
                                (habit_id, day.isoformat())).rowcount > 0

    def habits(self, today: date) -> list[Habit]:
        with self.db.connect() as conn:
            defs = conn.execute("SELECT id, name FROM habit_defs WHERE archived = 0 ORDER BY name").fetchall()
            checks: dict[int, set[date]] = {}
            for r in conn.execute("SELECT habit_id, day FROM habit_checks"):
                checks.setdefault(r["habit_id"], set()).add(date.fromisoformat(r["day"]))
        out = []
        for d in defs:
            days = checks.get(d["id"], set())
            current, best = streaks(days, today)
            week = sum(1 for k in range(7) if today - timedelta(days=k) in days)
            out.append(Habit(d["id"], d["name"], today in days, current, best, week))
        return out

    def grid(self, today: date, days: int = 14) -> dict[str, list[bool]]:
        """name -> done? for each of the last ``days`` days, oldest first (for the habits page)."""
        span = [today - timedelta(days=k) for k in range(days - 1, -1, -1)]
        with self.db.connect() as conn:
            rows = conn.execute("SELECT d.name, c.day FROM habit_defs d LEFT JOIN habit_checks c ON c.habit_id = d.id "
                                "WHERE d.archived = 0").fetchall()
        done: dict[str, set[str]] = {}
        for r in rows:
            done.setdefault(r["name"], set())
            if r["day"]:
                done[r["name"]].add(r["day"])
        return {name: [d.isoformat() in marks for d in span] for name, marks in sorted(done.items())}
