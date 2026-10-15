"""To-do list 2: priorities, repeating items, dependencies, postponing, overdue."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.tools import productivity as p
from app.tools.base import NoArgs, ToolContext, ToolError
from tests.test_v2 import db

NOW = datetime(2026, 9, 24, 9, 0)  # a Thursday


def ctx_for(tmp_path: Path, now: datetime = NOW) -> ToolContext:
    box = {"now": now}
    ctx = ToolContext(Settings(), database=db(tmp_path), clock=lambda: box["now"])
    ctx.box = box  # type: ignore[attr-defined]
    return ctx


def test_priority_and_category_show_in_the_list(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    p.add_todo(p.TodoArgs(text="pay rent", due="today", priority="low"), ctx)
    p.add_todo(p.TodoArgs(text="fix login bug", due="today", priority="high", category="TMS"), ctx)
    lines = p.list_todos(NoArgs(), ctx).message.splitlines()
    assert lines[1].startswith("1. fix login bug") and "[high]" in lines[1] and "#TMS" in lines[1]  # high first
    assert "[low]" in lines[2]
    p.update_todo(p.UpdateTodoArgs(item="rent", action="set_priority", priority="high"), ctx)
    assert "[low]" not in p.list_todos(NoArgs(), ctx).message


@pytest.mark.parametrize(("due", "repeat", "expected"), [
    ("2026-09-24", "daily", "2026-09-25"), ("2026-09-24", "weekly", "2026-10-01"),
    ("2026-09-25", "weekdays", "2026-09-28"),  # Friday -> Monday
    ("2026-01-31", "monthly", "2026-10-24"),   # long overdue: next month from today, never in the past
    ("2026-09-30", "monthly", "2026-10-30"), (None, "daily", None), ("2026-09-24", "", None)])
def test_next_due(due, repeat, expected) -> None:
    assert p.next_due(due, repeat, date(2026, 9, 24)) == expected


def test_monthly_end_of_month_clamps() -> None:
    assert p.next_due("2026-01-31", "monthly", date(2026, 1, 31)) == "2026-02-28"


def test_completing_a_repeating_item_creates_the_next_one_and_keeps_history(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    p.add_todo(p.TodoArgs(text="water plants", due="today", repeat="weekly"), ctx)
    result = p.complete_todo(p.TodoItemArgs(item="plants"), ctx)
    assert "next one Thu 01 Oct" in result.message
    [item] = p.open_todos(ctx)
    assert item["text"] == "water plants" and item["due"] == "2026-10-01" and item["repeat"] == "weekly"
    with ctx.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM todos WHERE done = 1").fetchone()[0] == 1  # history kept


def test_repeat_needs_a_due_day(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="needs a due day"):
        p.add_todo(p.TodoArgs(text="x y", repeat="daily"), ctx_for(tmp_path))


def test_dependencies(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    p.add_todo(p.TodoArgs(text="write the report"), ctx)
    p.add_todo(p.TodoArgs(text="send the report", after="write the report"), ctx)
    listing = p.list_todos(NoArgs(), ctx).message
    assert "send the report (waiting for: write the report)" in listing
    assert "write the report" in p.todo_summary(ctx).split("first: ")[1].split(";")[0]  # blocked item isn't suggested
    done = p.complete_todo(p.TodoItemArgs(item="write the report"), ctx)
    assert "now you can do: send the report" in done.message
    assert "waiting for" not in p.list_todos(NoArgs(), ctx).message


def test_completing_a_blocked_item_warns_but_is_allowed(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    p.add_todo(p.TodoArgs(text="write the report"), ctx)
    p.add_todo(p.TodoArgs(text="send the report", after="write the report"), ctx)
    assert "still open" in p.complete_todo(p.TodoItemArgs(item="send the report"), ctx).message


def test_postpone_counts_and_nags(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    p.add_todo(p.TodoArgs(text="call the bank", due="today"), ctx)
    for _ in range(2):
        assert "Moved" in p.update_todo(p.UpdateTodoArgs(item="bank", action="postpone"), ctx).message
    assert p.open_todos(ctx)[0]["due"] == "2026-09-26"
    assert "postponed 3 times" in p.update_todo(p.UpdateTodoArgs(item="bank", action="postpone"), ctx).message
    assert "Saturday" in p.update_todo(p.UpdateTodoArgs(item="bank", action="set_due", due="2026-10-03"), ctx).message
    with pytest.raises(ToolError):
        p.update_todo(p.UpdateTodoArgs(item="bank", action="set_priority"), ctx)


def test_overdue_is_counted(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    p.add_todo(p.TodoArgs(text="old thing", due="2026-09-20"), ctx)
    p.add_todo(p.TodoArgs(text="new thing", due="tomorrow"), ctx)
    assert "2 thing(s) on your list, 1 overdue" in p.list_todos(NoArgs(), ctx).message


def test_command_center_reports_only_real_things(tmp_path: Path) -> None:
    from app.core import command_center

    ctx = ctx_for(tmp_path)
    assert "Nothing on your plate" in command_center.describe(command_center.snapshot(ctx))
    p.add_todo(p.TodoArgs(text="old thing", due="2026-09-20"), ctx)
    p.add_todo(p.TodoArgs(text="today thing", due="today"), ctx)
    p.add_todo(p.TodoArgs(text="later thing", after="today thing"), ctx)
    p.complete_todo(p.TodoItemArgs(item="today thing"), ctx)
    text = p.command_center(p.CenterArgs(evening=True), ctx).message
    assert "2 open to-do(s), 1 overdue (old thing)" in text
    assert "today you completed 1 to-do(s) (today thing)" in text
    assert "Appointments aren't included" in text  # never pretends to know your appointments
    data = command_center.snapshot(ctx)
    assert data["calendar"] is None and data["unavailable"] == []


def test_calendar_events_are_included_only_when_a_calendar_answers(tmp_path: Path) -> None:
    from app.core import command_center

    ctx = ctx_for(tmp_path)
    events = [{"subject": "Standup", "start": "09:30", "end": "09:45", "location": ""},
              {"subject": "Review", "start": "14:00", "end": "15:00", "location": "Room 2"}]
    data = command_center.snapshot(ctx, lambda: events)
    text = command_center.describe(data)
    assert "2 appointment(s) today, first: Standup at 09:30" in text and "Appointments aren't included" not in text
    assert "no appointments today" in command_center.describe(command_center.snapshot(ctx, lambda: []))

    def broken() -> list:
        raise RuntimeError("Outlook is not installed")

    data = command_center.snapshot(ctx, broken)
    assert data["calendar"] is None and "Outlook is not installed" in data["calendar_note"]
    assert "Appointments aren't included: your calendar couldn't be read" in command_center.describe(data)


def test_the_tool_asks_outlook_and_reports_honestly_when_it_cannot(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    result = p.command_center(p.CenterArgs(), ctx)  # no Outlook here: the day is still reported, the gap is named
    assert result.ok and "Appointments aren't included: your calendar couldn't be read" in result.message
