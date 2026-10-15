"""3.5.0 Phase 2: goals, missions (plan -> validate -> simulate -> approve -> run with checkpoints -> recover),
dry run."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import Field

from app.agent.tool_manager import ToolExecutor, ToolRegistry
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core.goals import GoalStore, parse_deadline
from app.core.missions import MissionStore, validate_plan
from app.memory.database import Database
from app.security.permissions import PermissionEngine, RiskLevel
from app.tools import goals_missions
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from tests.test_tools import ScriptedConfirmer

TODAY = date(2026, 9, 26)  # a Saturday


@pytest.mark.parametrize("text, expected", [
    ("this month", "2026-09-30"), ("this week", "2026-09-27"), ("by friday", "2026-10-02"), ("tomorrow", "2026-09-27"),
    ("15 october", "2026-10-15"), ("October 15th", "2026-10-15"), ("in 2 weeks", "2026-10-10"), ("next month",
                                                                                                  "2026-10-31"),
    ("end of the year", "2026-12-31"), ("2026-11-01", "2026-11-01"), ("1 january", "2027-01-01"), (None, None),
])
def test_deadlines(text, expected) -> None:
    assert parse_deadline(text, TODAY) == expected


def test_bad_deadline() -> None:
    with pytest.raises(ToolError, match="didn't understand"):
        parse_deadline("whenever", TODAY)
    with pytest.raises(ToolError, match="not a real date"):
        parse_deadline("31 february", TODAY)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "j.db")
    database.initialize()
    return database


def clock():
    return datetime(2026, 9, 26, 9, 0).astimezone()


def test_goal_progress_follows_tasks(db) -> None:
    store = GoalStore(db, clock)
    goal = store.add("finish the TMS project", "2026-09-30", 1, tasks=["database", "login page", "tests"])
    goal, done = store.complete_task(goal, "login")
    assert done == "login page" and goal.progress == 33
    goal = store.add_task(goal, "deploy")
    assert goal.progress == 25 and "4 day(s) left" in goal.describe(TODAY) and "next: database" in goal.describe(TODAY)
    assert "Goals due soon: finish the TMS project" in store.briefing_line(TODAY)
    assert store.find("tms").id == goal.id and store.find("1").id == goal.id
    store.set_status(goal, "done")
    assert store.active() == [] and store.briefing_line(TODAY) == ""


# ------------------------------------------------------------------ missions with a real executor


class NameArgs(ToolArgs):
    name: str = Field(default="x", max_length=50)


def world():
    log: list[str] = []
    failures = {"flaky": 1}

    def safe(args, ctx):
        log.append(f"look:{args.name}")
        return ToolResult.success(f"looked at {args.name}")

    def flaky(args, ctx):
        if failures["flaky"]:
            failures["flaky"] -= 1
            return ToolResult.failure("timeout", "slow")
        log.append("flaky ok")
        return ToolResult.success("fine")

    def change(args, ctx):
        log.append(f"change:{args.name}")
        return ToolResult.success(f"changed {args.name}")

    def breaks(args, ctx):
        return ToolResult.failure("not_found", f"{args.name} isn't there")

    tools = [Tool("look", "read-only", safe, NameArgs), Tool("flaky", "read-only, flaky", flaky, NameArgs),
             Tool("change", "moderate", change, NameArgs, RiskLevel.MODERATE),
             Tool("remove", "dangerous", change, NameArgs, RiskLevel.DANGEROUS),
             Tool("breaks", "fails", breaks, NameArgs),
             *goals_missions.TOOLS]
    return tools, log


def make(db, confirm=True, moderate_asks=True):
    tools, log = world()
    confirmer = ScriptedConfirmer(confirm)
    ctx = ToolContext(Settings(), database=db, clock=clock)
    ex = ToolExecutor(ToolRegistry(tools), PermissionEngine(PermissionsConfig(confirm_moderate=moderate_asks)), ctx,
                      confirmer=confirmer)
    return ex, confirmer, log


def go(ex, name, **arguments):
    return asyncio.run(ex.execute(ToolCall(name, arguments))).result


def steps(*items):
    return [{"title": f"{tool} {name}", "tool": tool, "arguments": {"name": name}} for tool, name in items]


def test_plan_validation(db) -> None:
    ex, _, _ = make(db)
    with pytest.raises(ToolError, match="no tool named 'hack'"):
        validate_plan([{"tool": "hack"}], ex.registry, ex.context)
    with pytest.raises(ToolError, match="can't be part of a mission"):
        validate_plan([{"tool": "start_mission"}], ex.registry, ex.context)
    with pytest.raises(ToolError, match="Step 1 \\(look\\)"):
        validate_plan([{"tool": "look", "arguments": {"name": "x" * 99}}], ex.registry, ex.context)
    with pytest.raises(ToolError, match="at most"):
        validate_plan([{"tool": "look"}] * 13, ex.registry, ex.context)


def test_dry_run_changes_nothing(db) -> None:
    ex, confirmer, log = make(db)
    result = go(ex, "start_mission", title="prepare", dry_run=True, steps=steps(("look", "a"), ("change", "b"),
                                                                               ("remove", "c")))
    assert result.ok and result.message.startswith("Simulation (nothing done yet)")
    assert "3. remove c (asks you)" in result.message and "These still ask one by one: 3. remove c" in result.message
    assert log == [] and confirmer.requests == []
    assert MissionStore(db).unfinished().state == "planned"


def test_the_plan_is_approved_once_and_dangerous_steps_still_ask(db) -> None:
    ex, confirmer, log = make(db)
    result = go(ex, "start_mission", title="prepare the presentation",
                steps=steps(("look", "slides"), ("change", "notes"), ("remove", "old draft")))
    assert result.ok and "completed: 3 step(s)" in result.message
    assert log == ["look:slides", "change:notes", "change:old draft"]
    asked = [r.tool for r in confirmer.requests]
    assert asked == ["start_mission", "remove"]  # the plan, then the dangerous step itself - not the moderate one
    assert "Approve this plan?" in confirmer.requests[0].summary


def test_read_only_plans_just_run(db) -> None:
    ex, confirmer, log = make(db)
    assert go(ex, "start_mission", title="look around", steps=steps(("look", "a"), ("flaky", "b"))).ok
    assert confirmer.requests == [] and log == ["look:a", "flaky ok"]  # the flaky read-only step was retried once


def test_saying_no_to_the_plan_does_nothing(db) -> None:
    ex, _, log = make(db, confirm=False)
    result = go(ex, "start_mission", title="xy", steps=steps(("change", "a")))
    assert not result.ok and result.error == "declined" and log == []


def test_a_failure_pauses_and_can_be_skipped_or_retried(db) -> None:
    ex, confirmer, log = make(db)
    result = go(ex, "start_mission", title="tidy", steps=steps(("look", "a"), ("breaks", "b"), ("look", "c")))
    assert not result.ok and result.error == "mission_paused" and "Paused at step 2" in result.message
    assert "skip that step" in result.message and log == ["look:a"]  # a failed non-transient step isn't repeated
    status = go(ex, "mission_status")
    assert "'tidy': paused, 1/3" in status.message
    resumed = go(ex, "resume_mission", skip_failed=True)
    assert resumed.ok and log == ["look:a", "look:c"]  # step 1 isn't run twice
    assert MissionStore(db).unfinished() is None


def test_missions_survive_a_restart(db) -> None:
    ex, _, log = make(db)
    store = MissionStore(db)
    mission = store.create("long job", validate_plan(steps(("look", "a"), ("look", "b")), ex.registry, ex.context))
    mission.steps[0].state = "done"
    store.save_step(mission, mission.steps[0])
    store.set_state(mission, "running")
    assert store.mark_interrupted() == 1  # at start-up
    assert store.unfinished().note == "interrupted (Jarvis restarted)"
    assert go(ex, "resume_mission").ok and log == ["look:b"]


def test_cancel(db) -> None:
    ex, _, _ = make(db)
    go(ex, "start_mission", title="xy", dry_run=True, steps=steps(("look", "a")))
    assert "Cancelled 'xy'" in go(ex, "cancel_mission").message
    assert not go(ex, "cancel_mission").ok


def test_goal_tools(db) -> None:
    ex, confirmer, _ = make(db)
    assert go(ex, "set_goal", title="finish the TMS project", deadline="this month",
              tasks=["database", "login"]).message == "New goal: finish the TMS project, due 2026-09-30 with 2 task(s)."
    assert "Done: login" in go(ex, "update_goal", action="complete_task", text="login").message
    assert "50%" in go(ex, "list_goals").message
    go(ex, "update_goal", action="drop", goal="tms")
    assert confirmer.requests and confirmer.requests[-1].tool == "update_goal"


@pytest.mark.parametrize("said, tool, arguments", [
    ("my goal is to finish the TMS project this month", "set_goal",
     {"title": "finish the TMS project", "deadline": "this month"}),
    ("set a goal to learn Go by 15 October", "set_goal", {"title": "learn Go", "deadline": "by 15 October"}),
    ("what are my goals", "list_goals", {}),
    ("continue my mission", "resume_mission", {}),
    ("skip that step", "resume_mission", {"skip_failed": True}),
    ("cancel the mission", "cancel_mission", {}),
    ("how is my mission going", "mission_status", {}),
])
def test_goal_and_mission_commands(said, tool, arguments) -> None:
    from app.agent.shortcuts import match_shortcut

    call = match_shortcut(said, lambda n: None)
    assert call is not None and (call.name, dict(call.arguments)) == (tool, arguments)


def test_mission_control_shows_goals_and_missions(db) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.ui.mission_control import MissionControlDialog

    ex, _, _ = make(db)
    go(ex, "set_goal", title="finish the TMS project", tasks=["database"])
    go(ex, "start_mission", title="prepare", dry_run=True, steps=steps(("look", "a")))
    dialog = MissionControlDialog(db)
    items = [dialog.goal_list.item(i).text() for i in range(dialog.goal_list.count())]
    missions = [dialog.mission_list.item(i).text() for i in range(dialog.mission_list.count())]
    assert items[0].startswith("finish the TMS project: 0%") and "[ ] database" in items[1]
    assert missions[0].startswith("prepare - planned, 0/1") and "1. look a: pending" in missions[1]
