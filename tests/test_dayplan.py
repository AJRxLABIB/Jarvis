from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.core import dayplan
from app.tools import productivity as p
from app.tools.base import ToolContext
from tests.test_v2 import db

NOW = datetime(2026, 10, 14, 9, 0)  # a Wednesday, 9 am


def ctx_for(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(Settings(), database=db(tmp_path), clock=lambda: NOW)
    return ctx


def add(ctx, text, **kw):
    p.add_todo(p.TodoArgs(text=text, **kw), ctx)


def test_available_minutes_subtracts_calendar_once_and_interruptions() -> None:
    events = [{"subject": "Standup", "start": "Wed 14 Oct 10:00", "end": "Wed 14 Oct 11:00"},
              {"subject": "Overlap", "start": "Wed 14 Oct 10:30", "end": "Wed 14 Oct 11:30"}]
    assert dayplan.available_minutes(NOW) == 9 * 60
    assert dayplan.available_minutes(NOW, events) == 9 * 60 - 90  # 10:00-11:30 counted once
    assert dayplan.available_minutes(NOW, events, interruptions=60) == 9 * 60 - 150
    assert dayplan.available_minutes(NOW.replace(hour=19)) == 0
    assert any("overlaps" in c for c in dayplan.conflicts(events, NOW))


def test_order_reasons_blocking_and_budget(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    add(ctx, "Pay electricity bill", due="2026-10-12", minutes=20)
    add(ctx, "Write report", due="today", priority="high", minutes=120)
    add(ctx, "Read article", minutes=45, priority="low")
    add(ctx, "Send report", after="Write report", minutes=10)
    add(ctx, "Tidy desk")  # no estimate
    plan = dayplan.build(p.open_todos(ctx), NOW, 200)
    names = [i.text for i in plan.items]
    assert names[0] == "Pay electricity bill" and "overdue by 2 days" in plan.items[0].reason
    assert names[1] == "Write report" and "due today" in plan.items[1].reason
    assert plan.blocked[0]["item"] == "Send report" and "Send report" not in names
    tidy = next(i for i in plan.items if i.text == "Tidy desk")
    assert tidy.assumed and tidy.minutes == 30 and "estimate assumed" in tidy.reason
    assert plan.planned <= 200 + 0 and plan.not_today and plan.not_today[0]["item"] == "Read article"
    assert plan.next_action.text == "Pay electricity bill"


def test_urgent_items_are_flagged_not_hidden_when_time_is_short(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    add(ctx, "Big overdue job", due="2026-10-01", minutes=240)
    add(ctx, "Small thing", minutes=15)
    plan = dayplan.build(p.open_todos(ctx), NOW, 60)
    assert plan.items[0].over_budget and "too important to hide" in plan.items[0].reason
    assert any("over the time" in w for w in plan.warnings)
    assert "OVER BUDGET" in dayplan.describe(plan)


def test_repeated_postponement_is_called_out(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    add(ctx, "Call the bank", minutes=15)
    for _ in range(3):
        p.update_todo(p.UpdateTodoArgs(item="Call the bank", action="postpone"), ctx)
    plan = dayplan.build(p.open_todos(ctx), NOW, 120)
    assert any("postponed 3 times" in w for w in plan.warnings)


def test_empty_state(tmp_path: Path) -> None:
    plan = dayplan.build([], NOW, 100)
    assert "nothing to plan" in dayplan.describe(plan).lower()


def test_tool_plans_replans_and_reviews_without_duplicating(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    add(ctx, "Write report", due="today", priority="high", minutes=120)
    add(ctx, "Review code", due="today", minutes=60)
    add(ctx, "Read article", minutes=90, priority="low")
    first = p.plan_my_day(p.PlanArgs(available_minutes=240), ctx)
    assert first.data["next_action"]["text"] == "Write report" and len(p.open_todos(ctx)) == 3
    # an interruption removes 3 hours: replan
    again = p.plan_my_day(p.PlanArgs(available_minutes=70), ctx)
    assert "Replanned" in again.message and len(p.open_todos(ctx)) == 3  # no to-do was created or removed
    assert [i["text"] for i in again.data["items"]][0] == "Write report" and again.data["items"][0]["over_budget"]
    p.complete_todo(p.TodoItemArgs(item="Write report"), ctx)
    add(ctx, "Unplanned errand", minutes=10)
    p.complete_todo(p.TodoItemArgs(item="Unplanned errand"), ctx)
    review = p.plan_my_day(p.PlanArgs(review=True), ctx)
    assert review.data["completed"] == ["Write report"] and review.data["unplanned_done"] == ["Unplanned errand"]
    assert "none left over" in review.message and not review.data["not_done"]


def test_review_without_a_plan_and_calendar_note(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    assert p.plan_my_day(p.PlanArgs(review=True), ctx).data["planned"] is False
    add(ctx, "Something", minutes=10)
    result = p.plan_my_day(p.PlanArgs(), ctx)
    assert result.data["calendar_read"] is False and "could not read your calendar" in result.message


def test_set_minutes(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    add(ctx, "Draft proposal")
    p.update_todo(p.UpdateTodoArgs(item="Draft proposal", action="set_minutes", minutes=90), ctx)
    assert p.open_todos(ctx)[0]["minutes"] == 90
    with pytest.raises(Exception):
        p.update_todo(p.UpdateTodoArgs(item="Draft proposal", action="set_minutes"), ctx)


def test_shortcuts() -> None:
    from app.agent.shortcuts import match_shortcut

    def call(text):
        return match_shortcut(text, lambda n: None)

    assert call("plan my day").name == "plan_my_day" and call("plan my day").arguments == {}
    assert call("review my day").arguments == {"review": True}
    assert call("replan, I lost 2 hours").arguments == {"interruption_minutes": 120}
    assert call("replan my day").arguments == {"interruption_minutes": 0}
