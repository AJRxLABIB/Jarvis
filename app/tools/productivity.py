"""Everyday helpers: the clipboard, your to-do list, and meeting / lecture notes.

Clipboard: "translate what I copied into Bangla", "summarise my clipboard", "copy that".
Text that looks like a password, key or token is never read out or passed to the AI.
To-do list: "add buy milk to my list", "what's on my list", "mark buy milk done" (stored in the
local database; the morning briefing mentions it). Notes: "start lecture notes" - see
app.voice.meeting.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.phonetic import best_sounding
from app.utils.redaction import redact

CLIPBOARD_LIMIT = 8000


def looks_like_secret(text: str) -> bool:
    """One long "word" mixing letters, digits and symbols: a copied password or key."""
    value = text.strip()
    if not value or " " in value or "\n" in value or not 8 <= len(value) <= 256:
        return False
    if re.fullmatch(r"https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+|[A-Za-z]:\\\S*|[\d\s+()-]+", value):
        return False  # a link, an e-mail address, a path or a phone number
    kinds = sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w]"))
    return kinds >= 3


def read_clipboard(_: NoArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    text = winapi.get_clipboard_text()
    if not text.strip():
        return ToolResult.success("The clipboard has no text.", text="")
    if looks_like_secret(text):
        raise ToolError("What you copied looks like a password or key, so I won't read it.", "secret")
    cleaned = redact(text)
    clipped = cleaned[:CLIPBOARD_LIMIT]
    note = f" (first {CLIPBOARD_LIMIT} characters)" if len(cleaned) > CLIPBOARD_LIMIT else ""
    return ToolResult.success(f"Clipboard text{note}:\n{clipped}", text=clipped, characters=len(text))


class CopyArgs(ToolArgs):
    text: str = Field(min_length=1, max_length=20000, description="The text to put on the clipboard")


def copy_to_clipboard(args: CopyArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    winapi.set_clipboard_text(args.text)
    return ToolResult.success(f"Copied {len(args.text)} characters to the clipboard.")


# ------------------------------------------------------------------ to-do list

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_due(value: str | None, now: datetime) -> str | None:
    """ "today", "tomorrow", "friday", "2026-10-05" -> ISO date; None if not given; ToolError if unclear."""
    if not value or not value.strip():
        return None
    text = value.strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ToolError(f"'{value}' is not a real date.", "bad_date") from exc
    if text in ("today", "আজ", "আজকে"):
        return now.date().isoformat()
    if text in ("tomorrow", "কাল", "আগামীকাল"):
        return (now + timedelta(days=1)).date().isoformat()
    day = text.removeprefix("next ").removeprefix("on ")
    if day in _WEEKDAYS:
        ahead = (_WEEKDAYS.index(day) - now.weekday()) % 7 or 7
        return (now + timedelta(days=ahead)).date().isoformat()
    raise ToolError(f"I didn't understand the date '{value}'. Say today, tomorrow, a weekday or YYYY-MM-DD.",
                    "bad_date")


def _db(ctx: ToolContext):
    if ctx.database is None:
        raise ToolError("The to-do list needs the database.", "no_database")
    return ctx.database


def open_todos(ctx: ToolContext) -> list[dict]:
    with _db(ctx).connect() as conn:
        rows = conn.execute("SELECT id, text, due, priority, category, repeat, depends_on, postponed FROM todos "
                            "WHERE done = 0 ORDER BY due IS NULL, due, priority, id").fetchall()
    items = [dict(r) for r in rows]
    open_ids = {i["id"]: i["text"] for i in items}
    for item in items:  # blocked = it waits for another open item
        item["blocked_by"] = open_ids.get(item["depends_on"]) if item["depends_on"] else None
    return items


class TodoArgs(ToolArgs):
    text: str = Field(min_length=1, max_length=300, description="What to do, e.g. 'buy milk'")
    due: str | None = Field(default=None, max_length=20, description="today, tomorrow, a weekday or YYYY-MM-DD")
    priority: Literal["high", "normal", "low"] = Field(default="normal", description="How important it is")
    category: str = Field(default="", max_length=40, description="Optional group, e.g. work, home, TMS project")
    repeat: Literal["", "daily", "weekdays", "weekly", "monthly"] = Field(
        default="", description="Make it come back after it's done (needs a due day)")
    after: str | None = Field(default=None, max_length=300,
                              description="Another open item (number or words) that must be done first")


PRIORITY = {"high": 1, "normal": 2, "low": 3}
_REPEAT_DAYS = {"daily": 1, "weekly": 7}


def next_due(due: str | None, repeat: str, today) -> str | None:
    """The next date of a repeating item: after both its old due date and today (never in the past)."""
    if not repeat or not due:
        return None
    day = max(datetime.fromisoformat(due).date(), today)
    if repeat in _REPEAT_DAYS:
        return (day + timedelta(days=_REPEAT_DAYS[repeat])).isoformat()
    if repeat == "weekdays":
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return day.isoformat()
    if repeat == "monthly":
        month = day.month % 12 + 1
        year = day.year + (day.month == 12)
        for last in (31, 30, 29, 28):  # the 31st of a short month becomes its last day
            try:
                return day.replace(year=year, month=month, day=min(day.day, last)).isoformat()
            except ValueError:
                continue
    return None


def add_todo(args: TodoArgs, ctx: ToolContext) -> ToolResult:
    now = ctx.clock()
    due = parse_due(args.due, now)
    if args.repeat and not due:
        raise ToolError("A repeating item needs a due day (say when it's first due).", "bad_repeat")
    text = redact(args.text.strip())
    depends_on = _find(ctx, args.after)["id"] if args.after else None
    with _db(ctx).transaction() as conn:
        conn.execute("INSERT INTO todos (text, due, created_at, priority, category, repeat, depends_on) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (text, due, now.isoformat(), PRIORITY[args.priority],
                                                      " ".join(args.category.split()), args.repeat, depends_on))
    when = f" for {datetime.fromisoformat(due):%A %d %B}" if due else ""
    extra = (f", {args.priority} priority" if args.priority != "normal" else "") + \
        (f", repeats {args.repeat}" if args.repeat else "") + (", after the item it depends on" if depends_on else "")
    return ToolResult.success(f"Added '{text}' to your list{when}{extra}.", text=text, due=due)


def _describe(item: dict, number: int, now: datetime) -> str:
    due = ""
    if item["due"]:
        date = datetime.fromisoformat(item["due"]).date()
        due = " (today)" if date == now.date() else " (overdue)" if date < now.date() else f" ({date:%a %d %b})"
    marks = (" [high]" if item.get("priority") == 1 else " [low]" if item.get("priority") == 3 else "")
    marks += f" #{item['category']}" if item.get("category") else ""
    marks += f" (repeats {item['repeat']})" if item.get("repeat") else ""
    marks += f" (waiting for: {item['blocked_by']})" if item.get("blocked_by") else ""
    return f"{number}. {item['text']}{due}{marks}"


def list_todos(_: NoArgs, ctx: ToolContext) -> ToolResult:
    items = open_todos(ctx)
    if not items:
        return ToolResult.success("Your list is empty.", items=[])
    now = ctx.clock()
    lines = [_describe(item, n, now) for n, item in enumerate(items, 1)]
    overdue = sum(1 for i in items if i["due"] and i["due"] < now.date().isoformat())
    head = f"{len(items)} thing(s) on your list" + (f", {overdue} overdue" if overdue else "")
    return ToolResult.success(head + ":\n" + "\n".join(lines), items=items)


class TodoItemArgs(ToolArgs):
    item: str = Field(min_length=1, max_length=300, description="The item's number in the list, or its words")


def _find(ctx: ToolContext, wanted: str) -> dict:
    items = open_todos(ctx)
    if not items:
        raise ToolError("Your list is empty.", "empty")
    by_id = re.fullmatch(r"id:(\d{1,9})", wanted.strip().lower())
    if by_id:  # the window refers to an item by its id, which can't shift like a list number can
        found = next((i for i in items if i["id"] == int(by_id.group(1))), None)
        if found is None:
            raise ToolError("That item is no longer on your list.", "unknown")
        return found
    number = re.fullmatch(r"(?:number\s*)?#?(\d{1,3})", wanted.strip().lower())
    if number:
        index = int(number.group(1)) - 1
        if 0 <= index < len(items):
            return items[index]
        raise ToolError(f"There is no item {index + 1}; the list has {len(items)}.", "unknown")
    lowered = wanted.strip().lower()
    contains = [i for i in items if lowered in i["text"].lower() or i["text"].lower() in lowered]
    if len(contains) == 1:
        return contains[0]
    guess = best_sounding(lowered, [i["text"] for i in items], threshold=0.6)
    if guess is None:
        raise ToolError(f"I can't find '{wanted}' on your list.", "unknown")
    return next(i for i in items if i["text"] == guess)


def complete_todo(args: TodoItemArgs, ctx: ToolContext) -> ToolResult:
    item = _find(ctx, args.item)
    now = ctx.clock()
    again = next_due(item["due"], item["repeat"], now.date())
    with _db(ctx).transaction() as conn:
        conn.execute("UPDATE todos SET done = 1, done_at = ? WHERE id = ?", (now.isoformat(), item["id"]))
        if again:  # a repeating item: the next one is created, this one stays in the history
            conn.execute("INSERT INTO todos (text, due, created_at, priority, category, repeat, depends_on) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (item["text"], again, now.isoformat(), item["priority"],
                                                          item["category"], item["repeat"], None))
    notes = []
    if item.get("blocked_by"):
        notes.append(f"it was waiting for '{item['blocked_by']}', which is still open")
    unblocked = [i["text"] for i in open_todos(ctx) if i["depends_on"] == item["id"]]
    if unblocked:
        notes.append("now you can do: " + ", ".join(unblocked[:3]))
    if again:
        notes.append(f"next one {datetime.fromisoformat(again):%a %d %b}")
    left = len(open_todos(ctx))
    base = f"Done: {item['text']}. {left} left." if left else f"Done: {item['text']}. Your list is clear!"
    return ToolResult.success(base + (" (" + "; ".join(notes) + ")" if notes else ""), item=item, next_due=again)


class UpdateTodoArgs(TodoItemArgs):
    action: Literal["postpone", "set_due", "set_priority"] = Field(
        description="postpone = move it later (a day, or to 'due'); set_due; set_priority")
    due: str | None = Field(default=None, max_length=20, description="For set_due / postpone: today, tomorrow, a weekday "
                                                                      "or YYYY-MM-DD")
    priority: Literal["high", "normal", "low"] | None = Field(default=None, description="For set_priority")


def update_todo(args: UpdateTodoArgs, ctx: ToolContext) -> ToolResult:
    item = _find(ctx, args.item)
    now = ctx.clock()
    with _db(ctx).transaction() as conn:
        if args.action == "set_priority":
            if args.priority is None:
                raise ToolError("Which priority: high, normal or low?", "missing_priority")
            conn.execute("UPDATE todos SET priority = ? WHERE id = ?", (PRIORITY[args.priority], item["id"]))
            return ToolResult.success(f"'{item['text']}' is now {args.priority} priority.", item=item)
        if args.action == "set_due":
            due = parse_due(args.due, now)
            if due is None:
                raise ToolError("Which day? Say today, tomorrow, a weekday or YYYY-MM-DD.", "missing_due")
            conn.execute("UPDATE todos SET due = ? WHERE id = ?", (due, item["id"]))
            return ToolResult.success(f"'{item['text']}' is due {datetime.fromisoformat(due):%A %d %B}.", item=item)
        due = parse_due(args.due, now) or (max(datetime.fromisoformat(item["due"]).date() if item["due"] else
                                               now.date(), now.date()) + timedelta(days=1)).isoformat()
        conn.execute("UPDATE todos SET due = ?, postponed = postponed + 1 WHERE id = ?", (due, item["id"]))
    times = item["postponed"] + 1
    nag = f" (postponed {times} times - maybe break it up or drop it?)" if times >= 3 else ""
    return ToolResult.success(f"Moved '{item['text']}' to {datetime.fromisoformat(due):%A %d %B}.{nag}", item=item,
                              due=due)


def delete_todo(args: TodoItemArgs, ctx: ToolContext) -> ToolResult:
    item = _find(ctx, args.item)
    with _db(ctx).transaction() as conn:
        conn.execute("DELETE FROM todos WHERE id = ?", (item["id"],))
    return ToolResult.success(f"Removed '{item['text']}' from your list.", item=item)


def todo_summary(ctx: ToolContext) -> str:
    """For the morning briefing: "3 things on your list, first: ..." ("" when empty)."""
    try:
        items = open_todos(ctx)
    except Exception:
        return ""
    if not items:
        return ""
    now = ctx.clock()
    today = [i["text"] for i in items if i["due"] and i["due"] <= now.date().isoformat() and not i["blocked_by"]]
    first = today or [i["text"] for i in items if not i["blocked_by"]] or [i["text"] for i in items]
    head = f"You have {len(items)} thing{'s' if len(items) != 1 else ''} on your list"
    return f"{head}; {'due today' if today else 'first'}: {'; '.join(first[:3])}."


class CenterArgs(ToolArgs):
    evening: bool = Field(default=False, description="True for the end-of-day summary (what got done today)")


def command_center(args: CenterArgs, ctx: ToolContext) -> ToolResult:
    from app.core import command_center as center

    def calendar() -> list[dict]:
        from app.tools import outlook

        return outlook.calendar_events(outlook.CalendarArgs(day="today"), ctx).data["events"]

    data = center.snapshot(ctx, calendar)
    return ToolResult.success(center.describe(data, evening=args.evening), **data)


# ------------------------------------------------------------------ meeting notes

class NotesArgs(ToolArgs):
    action: Literal["start", "stop", "status"] = Field(description="start / stop taking notes of a meeting or lecture")


def meeting_notes(args: NotesArgs, ctx: ToolContext) -> ToolResult:
    control = getattr(ctx, "notes", None)
    if control is None:
        raise ToolError("Meeting notes work in the Jarvis window (not in this mode).", "unavailable")
    return ToolResult.success(control(args.action))


TOOLS: tuple[Tool, ...] = (
    Tool("read_clipboard", "The text the user copied (for 'translate / summarise / explain what I copied').",
         read_clipboard, NoArgs, RiskLevel.SAFE, category="clipboard", timeout=10),
    Tool("copy_to_clipboard", "Put text on the clipboard (e.g. a translation) so the user can paste it.",
         copy_to_clipboard, CopyArgs, RiskLevel.MODERATE, category="clipboard", timeout=10,
         confirmation=lambda a, _: f"Copy this to the clipboard? \"{a.text[:160]}\"",
         direct_without_confirmation=lambda a, _: True),
    Tool("add_todo", "Add something to the user's to-do list, optionally with a due day.", add_todo, TodoArgs,
         RiskLevel.MODERATE, category="todo", timeout=10, confirmation=lambda a, _: f"Add '{a.text}' to your list?",
         direct_without_confirmation=lambda a, _: True),
    Tool("list_todos", "What is on the user's to-do list.", list_todos, NoArgs, RiskLevel.SAFE, category="todo"),
    Tool("complete_todo", "Mark a to-do item done (by number or words).", complete_todo, TodoItemArgs,
         RiskLevel.MODERATE, category="todo", timeout=10, confirmation=lambda a, _: f"Mark '{a.item}' done?",
         direct_without_confirmation=lambda a, _: True),
    Tool("command_center", "Your day at a glance: overdue and due to-dos, blocked items, active missions and "
         "automations, what is waiting for you, and what you finished today ('how is my day', 'summarize what I "
         "completed today' -> evening=true).", command_center, CenterArgs, RiskLevel.SAFE, category="todo"),
    Tool("update_todo", "Change a to-do item: postpone it (a day or to a date), set its due day, or set its priority "
         "(high / normal / low).", update_todo, UpdateTodoArgs, RiskLevel.MODERATE, category="todo", timeout=10,
         confirmation=lambda a, _: f"Change '{a.item}' on your list?", direct_without_confirmation=lambda a, _: True),
    Tool("delete_todo", "Remove an item from the to-do list without doing it.", delete_todo, TodoItemArgs,
         RiskLevel.MODERATE, category="todo", timeout=10, confirmation=lambda a, _: f"Remove '{a.item}' from your list?",
         direct_without_confirmation=lambda a, _: True),
    Tool("meeting_notes", "Start or stop taking notes of a meeting, class or lecture: everything said is written "
         "down and summarised at the end.", meeting_notes, NotesArgs, RiskLevel.MODERATE, category="notes", timeout=15,
         confirmation=lambda a, _: "Start listening and taking notes?" if a.action == "start" else "Stop the notes?",
         direct_without_confirmation=lambda a, _: True),
)
