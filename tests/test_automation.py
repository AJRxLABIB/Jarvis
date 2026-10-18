from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecutor, ToolRegistry
from app.ai.base import ToolCall
from app.automation.scheduler import Scheduler
from app.automation.tasks import AutomationStore, Step, parse_steps, validate_steps
from app.automation.triggers import (
    BatteryTrigger, DailyTrigger, IntervalTrigger, ManualTrigger, OnceTrigger, once_at, parse_days, parse_time,
)
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.memory.database import Database
from app.security.permissions import PermissionEngine, RiskLevel
from app.tools.automation import DEFAULT_WORKFLOWS, seed_default_workflows
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.tools.catalog import build_default_registry
from tests.test_tools import ScriptedConfirmer

TZ = timezone(timedelta(hours=6))
NOW = datetime(2026, 9, 23, 15, 40, tzinfo=TZ)  # a Wednesday


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# ------------------------------------------------------------------- triggers


@pytest.mark.parametrize(("text", "expected"), [("11 PM", "23:00"), ("7:05am", "07:05"), ("18:30", "18:30"),
                                                ("12 am", "00:00"), ("12pm", "12:00"), ("noon", "12:00"), ("8", "08:00")])
def test_parse_time(text: str, expected: str) -> None:
    assert parse_time(text) == expected


@pytest.mark.parametrize("bad", ["25:00", "13 pm", "soon", "7:75"])
def test_bad_times(bad: str) -> None:
    with pytest.raises(ToolError):
        parse_time(bad)


def test_days_and_daily_next_run() -> None:
    assert parse_days("weekdays") == [0, 1, 2, 3, 4] and parse_days("mon, wed and fri") == [0, 2, 4]
    assert parse_days("every monday") == [0] and parse_days(None) == list(range(7))
    weekdays = DailyTrigger(time="8 am", days=parse_days("weekdays"))
    assert weekdays.next_run(NOW) == datetime(2026, 9, 24, 8, 0, tzinfo=TZ)  # Thursday
    friday_evening = datetime(2026, 9, 25, 20, 0, tzinfo=TZ)
    assert weekdays.next_run(friday_evening) == datetime(2026, 9, 28, 8, 0, tzinfo=TZ)  # skips the weekend
    assert DailyTrigger(time="23:00").next_run(NOW) == datetime(2026, 9, 23, 23, 0, tzinfo=TZ)
    assert weekdays.describe() == "on weekdays at 08:00"


def test_once_at() -> None:
    assert once_at(NOW, in_minutes=30).at == datetime(2026, 9, 23, 16, 10, tzinfo=TZ)
    assert once_at(NOW, time="3 pm").at.date().day == 24  # already past today -> tomorrow
    assert once_at(NOW, time="23:00").at.date().day == 23
    assert once_at(NOW, time="9:00", date="tomorrow").at == datetime(2026, 9, 24, 9, 0, tzinfo=TZ)
    with pytest.raises(ToolError, match="passed"):
        once_at(NOW, time="9:00", date="2026-09-23")


# ------------------------------------------------------------------- steps


def test_steps_are_validated_when_created() -> None:
    registry, ctx = build_default_registry(), ToolContext(Settings())
    good = parse_steps([{"tool": "open_application", "arguments": {"name": "chrome"}}, {"type": "notify", "message": "Hi"}])
    assert validate_steps(good, registry, ctx) == RiskLevel.SAFE
    for bad, match in (
        ([{"tool": "rm_rf", "arguments": {}}], "no tool"),
        ([{"tool": "open_application", "arguments": {}}], "name"),
        ([{"tool": "schedule_task", "arguments": {}}], "can't use"),  # no self-replicating schedules
        ([{"type": "notify", "message": "my password is hunter2"}], "secrets"),
        ([{"tool": "end_process", "arguments": {"name": "lsass.exe"}}], "blocked"),
        ([{"tool": "calculate", "arguments": {"expression": "1"}}] * 11, "At most"),
    ):
        with pytest.raises(ToolError, match=match):
            validate_steps(parse_steps(bad), registry, ctx)
    assert validate_steps(parse_steps([{"tool": "power_control", "arguments": {"action": "restart"}}]), registry, ctx) == RiskLevel.DANGEROUS
    assert parse_steps('[{"tool": "get_datetime"}]')[0].tool == "get_datetime"  # JSON text from small models


# ------------------------------------------------------------------- store + scheduler


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "a.db")
    database.initialize()
    return database


def recording_registry(calls: list, fail_on: set[str] = frozenset()) -> ToolRegistry:
    class Args(ToolArgs):
        name: str = "x"

    def make(tool_name: str):
        def handler(args, ctx):
            calls.append((tool_name, args.name))
            if tool_name in fail_on:
                return ToolResult.failure("boom", f"{tool_name} failed")
            return ToolResult.success(f"{tool_name} ok")
        return handler

    tools = [Tool(n, n, make(n), Args) for n in ("step_one", "step_two", "step_three")]
    tools.append(Tool("risky", "risky", make("risky"), Args, level=RiskLevel.DANGEROUS))
    return ToolRegistry(tools)


def scheduler_for(db: Database, calls: list, clock: Clock, *, fail_on=frozenset(), confirm=False, battery=None):
    notes: list[tuple[str, str]] = []
    confirmer = ScriptedConfirmer(confirm)
    ex = ToolExecutor(recording_registry(calls, set(fail_on)), PermissionEngine(PermissionsConfig()),
                      ToolContext(Settings(), database=db, clock=clock), confirmer=confirmer)
    scheduler = Scheduler(AutomationStore(db), ex, lambda t, m: notes.append((t, m)), clock=clock,
                          battery=battery or (lambda: None))
    return scheduler, notes, confirmer


def steps(*names: str, **kw) -> list[Step]:
    return [Step(type="tool", tool=n, arguments={"name": n}, **kw) for n in names]


def test_daily_task_runs_when_due_and_reschedules(db: Database) -> None:
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock)
    trigger = DailyTrigger(time="16:00")
    task, _ = scheduler.store.save("work apps", trigger, steps("step_one", "step_two"), trigger.next_run(NOW))
    assert asyncio.run(scheduler.tick()) == [] and calls == []  # not due yet
    clock.now = NOW.replace(hour=16, minute=0, second=10)
    [report] = asyncio.run(scheduler.tick())
    assert report.ok and calls == [("step_one", "step_one"), ("step_two", "step_two")]
    stored = scheduler.store.get(task.id)
    assert stored.last_status == "ok" and stored.next_run_at.startswith("2026-09-24T16:00") and stored.enabled
    assert asyncio.run(scheduler.tick()) == []  # doesn't run twice


def test_reminder_notifies_and_one_off_disables_itself(db: Database) -> None:
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock)
    trigger = once_at(NOW, in_minutes=5)
    task, _ = scheduler.store.save("Reminder: stretch", trigger, [Step(type="notify", message="Time to stretch")], trigger.at)
    clock.now = NOW + timedelta(minutes=5)
    asyncio.run(scheduler.tick())
    assert notes == [("Reminder: stretch", "Time to stretch")]
    stored = scheduler.store.get(task.id)
    assert not stored.enabled and stored.next_run_at is None


def test_failed_step_stops_the_workflow(db: Database) -> None:
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock, fail_on={"step_two"})
    task, _ = scheduler.store.save("chain", ManualTrigger(), steps("step_one", "step_two", "step_three"), None)
    report = asyncio.run(scheduler.run(task, manual=True))
    assert not report.ok and report.failed_step == 2 and [c[0] for c in calls] == ["step_one", "step_two"]
    assert "stopped at step 2" in notes[-1][1]
    calls.clear()
    lenient = [Step(type="tool", tool="step_two", arguments={}, continue_on_error=True), *steps("step_three")]
    task, _ = scheduler.store.save("lenient", ManualTrigger(), lenient, None)
    assert asyncio.run(scheduler.run(task)).ok and [c[0] for c in calls] == ["step_two", "step_three"]


def test_dangerous_steps_still_need_a_person(db: Database) -> None:
    calls, clock = [], Clock(NOW)
    scheduler, notes, confirmer = scheduler_for(db, calls, clock, confirm=False)  # nobody answers -> declined
    task, _ = scheduler.store.save("night", ManualTrigger(), steps("risky"), None)
    report = asyncio.run(scheduler.run(task))
    assert not report.ok and calls == [] and confirmer.requests[0].tool == "risky"


def test_catch_up_after_the_app_was_closed(db: Database) -> None:
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock)
    store = scheduler.store
    late, _ = store.save("recent", OnceTrigger(at=NOW - timedelta(minutes=20)), [Step(type="notify", message="late")],
                         NOW - timedelta(minutes=20))
    old, _ = store.save("ancient", OnceTrigger(at=NOW - timedelta(hours=5)), [Step(type="notify", message="old")],
                        NOW - timedelta(hours=5))
    daily, _ = store.save("daily", DailyTrigger(time="09:00"), [Step(type="notify", message="morning")],
                          NOW.replace(hour=9, minute=0) - timedelta(days=2))
    asyncio.run(scheduler.catch_up())
    asyncio.run(scheduler.tick())
    assert ("recent", "late") in notes  # up to an hour late still runs
    assert not store.get(old.id).enabled and "missed" in store.get(old.id).last_status
    assert ("daily", "morning") not in notes  # repeating tasks don't fire a backlog
    assert store.get(daily.id).next_run_at.startswith("2026-09-24T09:00")


def test_battery_trigger_fires_once_per_discharge(db: Database) -> None:
    calls, clock = [], Clock(NOW)
    reading = {"value": (50.0, False)}
    scheduler, notes, _ = scheduler_for(db, calls, clock, battery=lambda: reading["value"])
    scheduler.store.save("low battery", BatteryTrigger(percent=20), [Step(type="notify", message="Battery low")], None)
    asyncio.run(scheduler.tick())
    reading["value"] = (19.0, False)
    asyncio.run(scheduler.tick())
    asyncio.run(scheduler.tick())
    assert notes.count(("low battery", "Battery low")) == 1
    reading["value"] = (19.0, True)  # plugged in: re-armed
    asyncio.run(scheduler.tick())
    reading["value"] = (18.0, False)
    asyncio.run(scheduler.tick())
    assert notes.count(("low battery", "Battery low")) == 2


def test_paused_tasks_do_not_run(db: Database) -> None:
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock)
    task, _ = scheduler.store.save("x", IntervalTrigger(minutes=5), steps("step_one"), NOW)
    scheduler.store.set_enabled(task.id, False, None)
    assert asyncio.run(scheduler.tick()) == [] and calls == []


# ------------------------------------------------------------------- tools


def run_tool(db: Database, tool: str, confirm: bool = True, clock=None, **arguments):
    ctx = ToolContext(Settings(), database=db, clock=clock or Clock(NOW))
    confirmer = ScriptedConfirmer(confirm)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer)
    return asyncio.run(ex.execute(ToolCall(tool, arguments))), confirmer


def test_set_reminder_tool(db: Database) -> None:
    execution, confirmer = run_tool(db, "set_reminder", message="shut down the PC", time="11 PM")
    assert execution.result.ok and "once at 2026-09-23 23:00" in execution.result.message and not confirmer.requests
    execution, _ = run_tool(db, "set_reminder", message="stand up", time="10:00", repeat="weekdays")
    assert "on weekdays at 10:00" in execution.result.message
    assert run_tool(db, "set_reminder", message="x")[0].result.error == "missing_time"
    assert run_tool(db, "set_reminder", message="pin is 1234", in_minutes=5)[0].result.error in ("sensitive", "invalid_arguments")


def test_reminder_with_punctuation_gets_a_valid_name(db: Database) -> None:
    execution, _ = run_tool(db, "set_reminder", message="Call mom! Did you eat? #dinner", in_minutes=10)
    assert execution.result.ok
    names = [t.name for t in AutomationStore(db).all()]
    assert "Reminder: Call mom Did you eat dinner" in names
    execution, _ = run_tool(db, "set_reminder", message="!!!", in_minutes=10)
    assert execution.result.ok and "Reminder: note" in [t.name for t in AutomationStore(db).all()]


def test_schedule_task_asks_and_decline_saves_nothing(db: Database) -> None:
    steps_json = [{"tool": "open_application", "arguments": {"name": "chrome"}},
                  {"tool": "open_application", "arguments": {"name": "vs code"}}]
    execution, confirmer = run_tool(db, "schedule_task", confirm=False, name="work apps", steps=steps_json,
                                    time="8 am", days="weekdays")
    assert execution.result.error == "declined" and AutomationStore(db).all() == []
    assert "on weekdays at 08:00" in confirmer.requests[0].summary and "open_application(name='chrome')" in confirmer.requests[0].summary
    execution, _ = run_tool(db, "schedule_task", confirm=True, name="work apps", steps=steps_json, time="8 am", days="weekdays")
    assert execution.result.ok and AutomationStore(db).find("work apps").next_run_at.startswith("2026-09-24T08:00")


def test_schedule_with_invalid_steps_fails_before_asking(db: Database) -> None:
    execution, confirmer = run_tool(db, "schedule_task", name="bad", steps=[{"tool": "format_disk"}], time="8:00")
    assert execution.result.error == "invalid_steps" and not confirmer.requests


def test_run_workflow_and_manage(db: Database) -> None:
    run_tool(db, "save_workflow", name="morning", steps=[{"tool": "get_datetime"}, {"type": "notify", "message": "Good morning"}])
    execution, _ = run_tool(db, "run_workflow", name="morning")
    assert execution.result.ok and execution.result.data["steps"][0].startswith("get_datetime: ok")
    assert run_tool(db, "manage_automation", name="morning", action="pause")[0].result.ok
    listing = run_tool(db, "list_automations")[0].result
    assert listing.data["automations"][0]["enabled"] is False
    execution, confirmer = run_tool(db, "manage_automation", confirm=False, name="morning", action="delete")
    assert execution.result.error == "declined" and AutomationStore(db).all()
    assert run_tool(db, "manage_automation", confirm=True, name="morning", action="delete")[0].result.ok
    assert AutomationStore(db).all() == []


def test_default_workflow_is_seeded_once(db: Database) -> None:
    ctx = ToolContext(Settings(), database=db)
    seed_default_workflows(ctx)
    store = AutomationStore(db)
    assert [t.name for t in store.all()] == list(DEFAULT_WORKFLOWS)
    store.delete(store.all()[0].id)
    seed_default_workflows(ctx)
    assert store.all() == []  # a deleted default stays deleted
    validate_steps(parse_steps(DEFAULT_WORKFLOWS["development environment"]), build_default_registry(), ctx)


def test_workflow_direct_command() -> None:
    known = lambda name: "development environment" if name.lower() == "development environment" else None  # noqa: E731
    call = match_shortcut("start my development environment", lambda n: None, known)
    assert call == ToolCall("run_workflow", {"name": "development environment"})
    assert match_shortcut("start my car", lambda n: None, known) is None


def test_missed_repeats_are_reported_and_a_sleep_gap_does_not_burst(db: Database) -> None:
    """A daily task missed while Jarvis was closed is TOLD (not silently skipped); after the PC slept for a day, an
    old run is skipped with a notice instead of firing late, while one that is only minutes late still runs."""
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock)
    trigger = DailyTrigger(time="16:00")
    task, _ = scheduler.store.save("work apps", trigger, steps("step_one"), trigger.next_run(NOW))
    clock.now = NOW.replace(hour=20)                                     # closed through 16:00
    asyncio.run(scheduler.catch_up())
    assert notes and notes[-1][0] == "Missed" and "skipped, not run late" in notes[-1][1] and calls == []
    assert scheduler.store.get(task.id).next_run_at.startswith("2026-09-24T16:00")
    asyncio.run(scheduler.tick())                                       # the loop is running from here on
    clock.now = clock.now + timedelta(days=1, hours=2)                  # PC asleep through the next 16:00
    assert asyncio.run(scheduler.tick()) == [] and calls == []
    assert "while the PC was asleep" in notes[-1][1]
