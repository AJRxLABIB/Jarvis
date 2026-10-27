"""Reliability history for saved workflows and automations ("skills"), and suspending one that keeps failing.

Each finished run is recorded (name, time, ok, failed step, short message). When a skill fails ``SUSPEND_AFTER`` runs in a
row it is switched off - not deleted - with the reason written to its status, so a broken routine (a changed app, a missing
folder, a removed tool) stops running unattended instead of failing every day. You re-enable it yourself after fixing it.

Runs that say nothing about the skill itself are not counted: stopped by the emergency controls, declined by you, or
interrupted by a restart. A single success resets the streak. Nothing here widens a permission or runs anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.memory.database import utc_now_iso

SUSPEND_AFTER = 3
KEEP_ROWS = 50                       # per skill: bounded history
_NOT_THE_SKILLS_FAULT = ("emergency controls", "declined", "user_declined", "interrupted", "stopped by you", "missed")


@dataclass(frozen=True)
class Stats:
    name: str
    runs: int
    ok: int
    streak_failures: int
    last_failure: str

    @property
    def rate(self) -> float | None:
        return self.ok / self.runs if self.runs else None

    def describe(self) -> str:
        if not self.runs:
            return f"'{self.name}' has not run yet."
        text = f"'{self.name}': {self.ok} of {self.runs} recent runs succeeded ({self.ok * 100 // self.runs}%)"
        if self.streak_failures:
            text += f"; the last {self.streak_failures} failed" + (f" ({self.last_failure})" if self.last_failure else "")
        return text + "."


def counts(message: str) -> bool:
    low = (message or "").lower()
    return not any(marker in low for marker in _NOT_THE_SKILLS_FAULT)


def record(db: Any, name: str, ok: bool, failed_step: int | None = None, message: str = "") -> None:
    if not ok and not counts(message):
        return
    with db.transaction() as conn:
        conn.execute("INSERT INTO skill_runs (name, ts, ok, failed_step, message) VALUES (?, ?, ?, ?, ?)",
                     (name, utc_now_iso(), int(ok), failed_step, (message or "")[:300]))
        conn.execute("DELETE FROM skill_runs WHERE name = ? AND id NOT IN "
                     "(SELECT id FROM skill_runs WHERE name = ? ORDER BY id DESC LIMIT ?)", (name, name, KEEP_ROWS))


def stats(db: Any, name: str) -> Stats:
    with db.connect() as conn:
        rows = conn.execute("SELECT ok, message FROM skill_runs WHERE name = ? ORDER BY id DESC", (name,)).fetchall()
    streak, last = 0, ""
    for ok, message in rows:
        if ok:
            break
        streak += 1
        last = last or message
    return Stats(name, len(rows), sum(1 for ok, _ in rows if ok), streak, last)


def should_suspend(db: Any, name: str) -> Stats | None:
    s = stats(db, name)
    return s if s.streak_failures >= SUSPEND_AFTER else None


def clear(db: Any, name: str) -> None:
    """A fixed or re-enabled skill starts with a clean streak (its history is kept in the audit trail)."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM skill_runs WHERE name = ?", (name,))
