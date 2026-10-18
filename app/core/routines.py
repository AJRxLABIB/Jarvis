"""Routine suggester: notices what you keep doing at the same time and offers to make it an automation.

What it looks at: Jarvis's own audit log (tool name + the arguments you or the model used + when), only for a short list of
harmless "open / play / run" tools, only for requests that came from you (not from an automation or Jarvis itself).
What it needs before it says anything: the same action on at least 3 different days within the last 21 days, at times that
agree within 45 minutes. It then proposes a daily/weekday/weekend automation at the typical time.

What it never does: create anything by itself (``accept`` is a normal, confirmed tool call), suggest dangerous tools,
re-suggest something already automated or something you dismissed, or look at anything Jarvis did not record
(apps you opened by hand are invisible to it, and the suggestion says what it was based on).
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Only these can ever be suggested: they open or start something, change nothing in your files and are easy to undo.
ROUTINE_TOOLS = frozenset({"open_application", "open_website", "open_path", "open_in_editor", "play_music", "run_workflow"})
USER_SOURCES = frozenset({"voice", "text", "phone", "cli", None})
WINDOW_DAYS = 21
MIN_DAYS = 3
TOLERANCE_MINUTES = 45
MAX_SUGGESTIONS = 5


@dataclass(frozen=True)
class Routine:
    key: str
    tool: str
    arguments: dict[str, Any]
    time: str                 # HH:MM, rounded to 5 minutes
    days: tuple[int, ...]     # 0 = Monday
    occurrences: int          # how many days it matched
    first_seen: str
    last_seen: str

    def when(self) -> str:
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        if self.days == (0, 1, 2, 3, 4):
            day_text = "on weekdays"
        elif self.days == (5, 6):
            day_text = "on weekends"
        elif len(self.days) == 7:
            day_text = "every day"
        else:
            day_text = "on " + ", ".join(names[d] for d in self.days)
        return f"around {self.time} {day_text}"

    def describe(self) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in self.arguments.items())
        return (f"{self.tool}({args}) {self.when()} - you did this on {self.occurrences} different days "
                f"between {self.first_seen} and {self.last_seen}")


def args_key(tool: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps({k: (v.lower().strip() if isinstance(v, str) else v) for k, v in sorted(arguments.items())},
                           ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(f"{tool}|{canonical}".encode(), usedforsecurity=False).hexdigest()[:10]


def load(database, now: datetime, window_days: int = WINDOW_DAYS) -> list[tuple[datetime, str, dict[str, Any]]]:
    """(local time, tool, arguments) of successful, user-requested routine-tool calls in the window."""
    since = (now - timedelta(days=window_days)).astimezone(timezone.utc).isoformat(timespec="seconds")
    marks = ",".join("?" for _ in ROUTINE_TOOLS)
    with database.connect() as conn:
        rows = conn.execute(
            f"SELECT a.ts, a.tool, a.details, r.source FROM audit_log a LEFT JOIN agent_runs r ON r.id = a.run_id "
            f"WHERE a.event = 'tool_result' AND a.ts >= ? AND a.tool IN ({marks}) ORDER BY a.id",
            (since, *sorted(ROUTINE_TOOLS))).fetchall()
    out = []
    for ts, tool, details, source in rows:
        if source not in USER_SOURCES:
            continue
        try:
            data = json.loads(details or "{}")
            at = datetime.fromisoformat(ts).astimezone(now.tzinfo)
        except ValueError:
            continue
        arguments = data.get("arguments")
        if data.get("ok") and isinstance(arguments, dict) and arguments:
            out.append((at, tool, arguments))
    return out


def _cluster(minutes: list[int]) -> list[int]:
    """The largest group of times that all lie within TOLERANCE_MINUTES of their median."""
    best: list[int] = []
    ordered = sorted(minutes)
    for i, start in enumerate(ordered):
        group = [m for m in ordered[i:] if m - start <= 2 * TOLERANCE_MINUTES]
        if len(group) > len(best):
            best = group
    if not best:
        return []
    middle = statistics.median(best)
    return [m for m in best if abs(m - middle) <= TOLERANCE_MINUTES]


def _days(seen: set[int]) -> tuple[int, ...]:
    if seen <= {0, 1, 2, 3, 4} and len(seen) >= 3:
        return (0, 1, 2, 3, 4)
    if seen <= {5, 6}:
        return (5, 6)
    if len(seen) >= 5:
        return tuple(range(7))
    return tuple(sorted(seen))


def detect(actions: list[tuple[datetime, str, dict[str, Any]]], *, skip_keys: set[str] = frozenset(),
           eligible=lambda tool: True) -> list[Routine]:
    """Routines found in ``actions`` (see load), best first. ``skip_keys``: dismissed or already automated."""
    groups: dict[str, list[tuple[datetime, str, dict[str, Any]]]] = defaultdict(list)
    for item in actions:
        if item[1] in ROUTINE_TOOLS and eligible(item[1]):
            groups[args_key(item[1], item[2])].append(item)
    found = []
    for key, items in groups.items():
        if key in skip_keys:
            continue
        per_day: dict[Any, datetime] = {}
        for at, _, _ in items:
            per_day.setdefault(at.date(), at)                      # the first time that day
        if len(per_day) < MIN_DAYS:
            continue
        minutes = {d: at.hour * 60 + at.minute for d, at in per_day.items()}
        agreeing = _cluster(list(minutes.values()))
        matching = [d for d, m in minutes.items() if m in agreeing]
        if len(matching) < MIN_DAYS:
            continue
        typical = int(round(statistics.median([minutes[d] for d in matching]) / 5) * 5) % (24 * 60)
        weekdays = {d.weekday() for d in matching}
        found.append(Routine(key, items[0][1], items[0][2], f"{typical // 60:02d}:{typical % 60:02d}", _days(weekdays),
                             len(matching), min(matching).isoformat(), max(matching).isoformat()))
    return sorted(found, key=lambda r: (-r.occurrences, r.key))[:MAX_SUGGESTIONS]


def automated_keys(store) -> set[str]:
    """Keys of single-tool steps that a saved automation or workflow already runs."""
    keys = set()
    for task in store.all():
        for step in task.steps:
            if step.type == "tool" and step.tool in ROUTINE_TOOLS and step.arguments:
                keys.add(args_key(step.tool, step.arguments))
    return keys


DISMISSED = "routine_dismissed"


def dismissed(kv) -> set[str]:
    value = kv.get(DISMISSED, [])
    return {str(v) for v in value} if isinstance(value, list) else set()


def dismiss(kv, key: str) -> None:
    keys = sorted(dismissed(kv) | {key})[-200:]
    kv.set(DISMISSED, keys)
