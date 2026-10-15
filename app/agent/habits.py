"""Habits: "You usually open VS Code around now - shall I open it?" (PROACTIVE_SUGGESTIONS).

Only the name of each app you open through Jarvis and the local time are stored, on this PC.
A suggestion needs the same app opened around the same time of day on at least MIN_DAYS of
the last LOOKBACK_DAYS days, and each app is suggested at most once a day.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.memory.database import Database

WINDOW_MINUTES = 20
MIN_DAYS = 3
LOOKBACK_DAYS = 10
KEEP_DAYS = 60


def record(database: Database, name: str, when: datetime) -> None:
    with database.transaction() as conn:
        conn.execute("INSERT INTO usage_events (name, day, weekday, minute) VALUES (?, ?, ?, ?)",
                     (name, when.strftime("%Y-%m-%d"), when.weekday(), when.hour * 60 + when.minute))
        conn.execute("DELETE FROM usage_events WHERE day < ?", ((when - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d"),))


def suggestions(database: Database, now: datetime, *, skip: set[str] = frozenset()) -> list[str]:
    """Apps you open around this time on most days (most regular first), not yet used today."""
    minute = now.hour * 60 + now.minute
    today = now.strftime("%Y-%m-%d")
    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT name, COUNT(DISTINCT day) FROM usage_events WHERE day >= ? AND day < ? AND ABS(minute - ?) <= ? "
            "GROUP BY name HAVING COUNT(DISTINCT day) >= ? ORDER BY COUNT(DISTINCT day) DESC",
            (since, today, minute, WINDOW_MINUTES, MIN_DAYS)).fetchall()
        used_today = {r[0] for r in conn.execute("SELECT DISTINCT name FROM usage_events WHERE day = ?", (today,))}
    return [name for name, _ in rows if name not in used_today and name not in skip]


def routine_question(apps: list[str], address: str = "") -> str:
    """One app: "You usually open VS Code around now. Shall I open it?"; several: your usual set, all at once."""
    polite = f", {address}" if address else ""
    if len(apps) == 1:
        return f"You usually open {apps[0]} around now{polite}. Shall I open it?"
    listed = ", ".join(apps[:-1]) + " and " + apps[-1]
    return f"You usually open {listed} around now{polite}. Shall I open them?"
