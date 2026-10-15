"""What you did with Jarvis, and picking it back up.

"What was I doing before lunch?" / "what did I do yesterday?" -> a summary of the actions Jarvis
carried out for you in that time (from its audit log: successful tool calls only).
"Continue where I left off" -> the apps, websites and folders from your last session, opened again.

Honest by design: only what Jarvis itself did is known. Apps you opened by hand are not in the log,
and the answer says so rather than guessing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from app.ai.base import ToolCall

SESSION_GAP = timedelta(hours=2)  # a pause this long starts a new session
MAX_REOPEN = 8
# tool -> (what it means in a summary, the reopen call or None)
_OPENERS = ("open_application", "open_website", "open_path", "open_in_editor", "run_workflow")
_SUMMARY_TOOLS = (*_OPENERS, "play_music", "close_application", "web_search", "find_files", "take_screenshot",
                  "add_todo", "write_document", "power_control", "whatsapp_message")


@dataclass(frozen=True)
class Action:
    at: datetime  # local time
    tool: str
    arguments: dict[str, Any]
    message: str


def load(database, start: datetime, end: datetime) -> list[Action]:
    """Successful actions between ``start`` and ``end`` (aware datetimes), oldest first."""
    lo = start.astimezone(timezone.utc).isoformat(timespec="seconds")
    hi = end.astimezone(timezone.utc).isoformat(timespec="seconds")
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT ts, tool, details FROM audit_log WHERE event = 'tool_result' AND ts >= ? AND ts < ? ORDER BY ts, id",
            (lo, hi)).fetchall()
    local = end.tzinfo
    found = []
    for ts, tool, details in rows:
        try:
            data = json.loads(details or "{}")
            at = datetime.fromisoformat(ts).astimezone(local)
        except ValueError:
            continue
        if tool in _SUMMARY_TOOLS and data.get("ok"):
            arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
            found.append(Action(at, tool, arguments, str(data.get("message") or "")))
    return found


# ------------------------------------------------------------------ periods you can ask about

def period(phrase: str, now: datetime) -> tuple[datetime, datetime, str] | None:
    """'before lunch' -> (today 05:00, today 13:00, 'this morning'). ``now`` is aware local time."""
    text = " ".join((phrase or "").lower().split())
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def at(base: datetime, hour: int) -> datetime:
        return base.replace(hour=hour) if hour < 24 else base + timedelta(days=1)

    yesterday = day - timedelta(days=1)
    hours = re.search(r"(?:last|past)\s+(\d{1,2}|an?|one|two|three)\s+hours?", text)
    if hours:
        count = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}.get(hours.group(1)) or int(hours.group(1))
        return now - timedelta(hours=count), now, f"in the last {count} hour{'s' if count > 1 else ''}"
    if re.search(r"last hour|past hour", text):
        return now - timedelta(hours=1), now, "in the last hour"
    if "yesterday" in text:
        if "morning" in text:
            return at(yesterday, 5), at(yesterday, 12), "yesterday morning"
        if "afternoon" in text:
            return at(yesterday, 12), at(yesterday, 17), "yesterday afternoon"
        if "evening" in text or "night" in text:
            return at(yesterday, 17), at(yesterday, 24), "yesterday evening"
        return yesterday, day, "yesterday"
    if "last night" in text:
        return at(yesterday, 18), at(day, 5), "last night"
    if "before lunch" in text or "this morning" in text or text == "morning":
        return at(day, 5), min(now, at(day, 13)), "this morning"
    if "after lunch" in text or "this afternoon" in text:
        return at(day, 12), min(now, at(day, 17)), "this afternoon"
    if "this evening" in text or "tonight" in text:
        return at(day, 17), now, "this evening"
    if "this week" in text:
        return day - timedelta(days=now.weekday()), now, "this week"
    if "last week" in text or "previous week" in text:
        start = day - timedelta(days=now.weekday() + 7)
        return start, start + timedelta(days=7), "last week"
    if "this month" in text:
        return day.replace(day=1), now, "this month"
    if "last month" in text or "previous month" in text:
        end = day.replace(day=1)
        return (end - timedelta(days=1)).replace(day=1), end, "last month"
    days = re.search(r"(?:last|past)\s+(\d{1,2}|two|three|seven)\s+days", text)
    if days:
        count = {"two": 2, "three": 3, "seven": 7}.get(days.group(1)) or int(days.group(1))
        return day - timedelta(days=count), now, f"in the last {count} days"
    if "today" in text or not text:
        return day, now, "today"
    return None


# ------------------------------------------------------------------ sessions

def sessions(actions: list[Action]) -> list[list[Action]]:
    """Actions split wherever there is a pause of SESSION_GAP or more."""
    groups: list[list[Action]] = []
    for action in actions:
        if groups and action.at - groups[-1][-1].at < SESSION_GAP:
            groups[-1].append(action)
        else:
            groups.append([action])
    return groups


def last_session(database, now: datetime, started_at: datetime | None = None,
                 lookback: timedelta = timedelta(days=7)) -> list[Action]:
    """Your previous session: the last group of actions before this start of Jarvis, or - without a
    start time - before the current group (the one still going on)."""
    actions = load(database, now - lookback, now)
    if started_at is not None:
        actions = [a for a in actions if a.at < started_at]
        groups = sessions(actions)
        return groups[-1] if groups else []
    groups = sessions(actions)
    if groups and now - groups[-1][-1].at < SESSION_GAP:
        groups = groups[:-1]  # still going on: that's now, not "where I left off"
    return groups[-1] if groups else []


def current_session(database, now: datetime, lookback: timedelta = timedelta(days=1)) -> list[Action]:
    """What you've been doing just now (the group of actions still going on)."""
    groups = sessions(load(database, now - lookback, now))
    return groups[-1] if groups and now - groups[-1][-1].at < SESSION_GAP else []


def as_steps(calls: list[ToolCall]) -> list[dict[str, Any]]:
    return [{"tool": c.name, "arguments": dict(c.arguments)} for c in calls]


def reopen_calls(session: list[Action]) -> list[ToolCall]:
    """What to open again: apps not closed later in that session, sites, folders, projects (in order, no repeats)."""
    closed_after: dict[str, datetime] = {}
    for action in session:
        if action.tool == "close_application":
            closed_after[_key(action.arguments.get("name"))] = action.at
    calls: list[ToolCall] = []
    seen: set[str] = set()
    for action in session:
        if action.tool not in _OPENERS:
            continue
        args = {k: v for k, v in action.arguments.items() if k in ("name", "profile", "site", "path", "project")
                and isinstance(v, str) and v.strip()}
        if not args:
            continue
        name = _key(args.get("name") or args.get("site") or args.get("path") or args.get("project"))
        if action.tool == "open_application" and closed_after.get(name, action.at) > action.at:
            continue  # you closed it again
        key = f"{action.tool}:{name}"
        if key in seen:
            continue
        seen.add(key)
        calls.append(ToolCall(action.tool, args))
    return calls[:MAX_REOPEN]


def _key(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


# ------------------------------------------------------------------ the summary

def summarize(actions: list[Action], label: str) -> str:
    if not actions:
        return (f"I didn't do anything for you {label}. (I only know what I did myself - apps you opened by hand "
                "aren't in my log.)")
    apps, sites, music, searches, other = [], [], [], [], []

    def add(bucket: list[str], item: str) -> None:
        if item and item not in bucket:
            bucket.append(item)

    for a in actions:
        if a.tool == "open_application":
            add(apps, str(a.arguments.get("name") or "").strip())
        elif a.tool == "open_website":
            add(sites, str(a.arguments.get("site") or "").strip())
        elif a.tool == "play_music":
            add(music, str(a.arguments.get("query") or "").strip())
        elif a.tool == "web_search":
            add(searches, str(a.arguments.get("query") or "").strip())
        elif a.tool not in ("close_application",):
            add(other, a.message.rstrip("."))
    first, last = actions[0].at, actions[-1].at
    when = (f"between {first:%H:%M} and {last:%H:%M}" if (last - first) >= timedelta(minutes=5)
            else f"around {first:%H:%M}")
    if (last.date() != first.date()):
        when = f"from {first:%a %H:%M} to {last:%a %H:%M}"
    parts = []
    if apps:
        parts.append("opened " + _list(apps))
    if sites:
        parts.append("went to " + _list(sites))
    if music:
        parts.append("played " + _list(music, 3))
    if searches:
        parts.append("searched for " + _list(searches, 3))
    if other:
        parts.append(_list(other, 3).lower() if len(other) > 1 else other[0][:1].lower() + other[0][1:])
    return f"{label[:1].upper() + label[1:]}, {when}, I " + "; ".join(parts) + "."


def _list(items: list[str], limit: int = 6) -> str:
    items = [i for i in items if i][:limit]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def today_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), time(0), tzinfo=now.tzinfo)
