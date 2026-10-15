"""3.21: Missions 2.0 (dependencies, milestones, checkpoints, rollback) and the background task center."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import Field

from app.agent.tool_manager import ToolExecutor, ToolRegistry
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import task_center
from app.core.missions import MissionStore, validate_plan
from app.memory.database import Database
from app.security.permissions import PermissionEngine, RiskLevel
from app.tools import goals_missions
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from tests.test_tools import ScriptedConfirmer


class NameArgs(ToolArgs):
    name: str = Field(default="x", max_length=50)


class PathArgs(ToolArgs):
    path: str = Field(max_length=200)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "j.db")
    database.initialize()
    return database


def make(db, confirm=True):
    log: list[str] = []

    def look(args, ctx):
        log.append(f"look:{args.name}")
        return ToolResult.success(f"looked at {args.name}")

    def create(args, ctx):
        log.append(f"create:{args.name}")
        return ToolResult.success(f"made {args.name}", path=f"C:/work/{args.name}.txt")

    def delete(args, ctx):
        log.append(f"delete:{args.path}")
        return ToolResult.success("moved to the Recycle Bin")

    def breaks(args, ctx):
        return ToolResult.failure("not_found", f"{args.name} isn't there")

    tools = [Tool("look", "read-only", look, NameArgs), Tool("create_item", "make a file", create, NameArgs,
                                                               RiskLevel.MODERATE),
             Tool("delete_item", "bin a file", delete, PathArgs, RiskLevel.MODERATE),
             Tool("breaks", "fails", breaks, NameArgs), *goals_missions.TOOLS]
    confirmer = ScriptedConfirmer(confirm)
    ctx = ToolContext(Settings(), database=db)
    ex = ToolExecutor(ToolRegistry(tools), PermissionEngine(PermissionsConfig(confirm_moderate=True)), ctx,
                      confirmer=confirmer)
    return ex, confirmer, log


def go(ex, name, **arguments):
    return asyncio.run(ex.execute(ToolCall(name, arguments))).result


def step(tool, name, **extra):
    return {"title": f"{tool} {name}", "tool": tool, "arguments": {"name": name}, **extra}


# ------------------------------------------------------------------ plan validation


def test_dependencies_must_point_backwards(db) -> None:
    ex, _, _ = make(db)
    with pytest.raises(ToolError):
        validate_plan([step("look", "a", depends_on=[2]), step("look", "b")], ex.registry, ex.context)
    with pytest.raises(ToolError):
        validate_plan([step("look", "a", depends_on=[1])], ex.registry, ex.context)
    plan = validate_plan([step("look", "a", milestone="  Research  "), step("look", "b", depends_on=[1, 1],
                                                                              checkpoint=True)],
                         ex.registry, ex.context)
    assert plan[0].milestone == "Research" and plan[1].depends_on == [1] and plan[1].checkpoint


def test_new_step_fields_survive_the_database(db) -> None:
    ex, _, _ = make(db)
    store = MissionStore(db)
    plan = validate_plan([step("look", "a", milestone="M1"), step("look", "b", depends_on=[1], checkpoint=True)],
                         ex.registry, ex.context)
    again = store.get(store.create("x", plan).id)
    assert [(s.milestone, s.depends_on, s.checkpoint) for s in again.steps] == [("M1", [], False), ("", [1], True)]
    assert again.updated_at


# ------------------------------------------------------------------ running


def test_checkpoint_pauses_until_you_continue(db) -> None:
    ex, confirmer, log = make(db)
    result = go(ex, "start_mission", title="ship it",
                steps=[step("look", "a"), step("look", "b", checkpoint=True), step("look", "c")])
    assert not result.ok and "Checkpoint before step 2" in result.message and log == ["look:a"]
    mission = MissionStore(db).unfinished()
    assert mission.state == "paused" and mission.steps[1].state == "waiting"
    assert task_center.mission_status(mission) == "WAITING_FOR_APPROVAL"
    assert go(ex, "resume_mission").ok and log == ["look:a", "look:b", "look:c"]
    assert MissionStore(db).recent(1)[0].state == "completed"


def test_a_skipped_step_skips_what_depends_on_it(db) -> None:
    ex, _, log = make(db)
    result = go(ex, "start_mission", title="chain",
                steps=[step("breaks", "a"), step("look", "b", depends_on=[1]), step("look", "c")])
    assert not result.ok
    assert go(ex, "resume_mission", skip_failed=True).ok
    mission = MissionStore(db).recent(1)[0]
    assert [s.state for s in mission.steps] == ["skipped", "skipped", "done"] and log == ["look:c"]
    assert "needs step 1" in mission.steps[1].result


def test_status_shows_milestones(db) -> None:
    ex, _, _ = make(db)
    go(ex, "start_mission", title="talk", steps=[step("look", "a", milestone="Research"),
                                                step("look", "b", milestone="Research"),
                                                step("look", "c", milestone="Slides")])
    status = go(ex, "mission_status")
    assert "milestones: Research 2/2, Slides 1/1" in status.message
    assert status.data["missions"][0]["milestones"][0] == {"name": "Research", "done": 2, "total": 2}


def test_rollback_undoes_newest_first_and_names_what_it_cannot(db) -> None:
    ex, confirmer, log = make(db)
    assert go(ex, "start_mission", title="drafts",
              steps=[step("create_item", "one"), step("look", "x"), step("create_item", "two")]).ok
    log.clear()
    confirmer.requests.clear()
    result = go(ex, "rollback_mission")
    assert result.ok and "2 step(s) undone" in result.message and "2 (look x)" in result.message
    assert log == ["delete:C:/work/two.txt", "delete:C:/work/one.txt"]
    assert confirmer.requests[0].tool == "rollback_mission"  # always asked first
    assert "2 of 3 finished step(s) can be undone" in confirmer.requests[0].summary
    mission = MissionStore(db).recent(1)[0]
    assert mission.state == "rolled_back" and [s.state for s in mission.steps] == ["undone", "done", "undone"]
    assert task_center.mission_status(mission) == "ROLLED_BACK"
    assert not go(ex, "rollback_mission").ok  # nothing undoable left... except the read-only step, which can't


def test_saying_no_to_rollback_changes_nothing(db) -> None:
    ex, confirmer, log = make(db)
    assert go(ex, "start_mission", title="drafts d", steps=[step("create_item", "one")]).ok
    log.clear()
    confirmer.answer = False
    result = go(ex, "rollback_mission")
    assert not result.ok and log == [] and MissionStore(db).recent(1)[0].state == "completed"


def test_rollback_is_never_a_mission_step(db) -> None:
    ex, _, _ = make(db)
    for name in ("rollback_mission", "task_center"):
        with pytest.raises(ToolError, match="can't be part of a mission"):
            validate_plan([{"tool": name}], ex.registry, ex.context)


# ------------------------------------------------------------------ task center


def test_task_center_lists_missions_with_plain_statuses(db) -> None:
    ex, _, _ = make(db)
    go(ex, "start_mission", title="later", dry_run=True, steps=[step("look", "a")])
    go(ex, "start_mission", title="broken", steps=[step("breaks", "a")])
    tasks = task_center.collect(db)
    by_title = {t.title: t for t in tasks}
    assert by_title["later"].status == "QUEUED" and by_title["broken"].status == "PAUSED"
    assert "continue my mission" in by_title["broken"].commands
    assert all(t.status in task_center.STATUSES for t in tasks)
    result = go(ex, "task_center")
    assert result.ok and "active background task(s)" in result.message and result.data["counts"]["PAUSED"] == 1


def test_task_center_hides_finished_on_request(db) -> None:
    ex, _, _ = make(db)
    go(ex, "start_mission", title="done one", steps=[step("look", "a")])
    assert [t.status for t in task_center.collect(db)] == ["COMPLETED"]
    assert task_center.collect(db, include_finished=False) == []
    assert task_center.describe([]) == "Nothing is running or queued in the background."


def test_task_center_includes_automations(db) -> None:
    from datetime import datetime, timedelta

    from app.automation.tasks import AutomationStore, parse_steps

    from app.automation.triggers import DailyTrigger

    store = AutomationStore(db)
    trigger = DailyTrigger(time="08:00")
    store.save("morning", trigger, parse_steps([{"tool": "get_weather", "arguments": {}}]),
               datetime.now().astimezone() + timedelta(hours=1))
    tasks = [t for t in task_center.collect(db) if t.kind == "automation"]
    assert tasks and tasks[0].title == "morning" and tasks[0].status == "QUEUED"


@pytest.mark.parametrize(("said", "tool"), [
    ("roll back the mission", "rollback_mission"),
    ("undo my last mission", "rollback_mission"),
    ("show background tasks", "task_center"),
    ("open the task center", "task_center"),
    ("what's running in the background", "task_center"),
])
def test_commands(said, tool) -> None:
    from app.agent.shortcuts import match_shortcut

    call = match_shortcut(said, lambda n: None)
    assert call is not None and call.name == tool


def test_task_center_dialog(db) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.ui.task_center import TaskCenterDialog

    ex, _, _ = make(db)
    go(ex, "start_mission", title="broken", steps=[step("breaks", "a")])
    sent: list[str] = []
    dialog = TaskCenterDialog(db, sent.append)
    assert dialog.table.rowCount() == 1 and dialog.table.item(0, 0).text() == "PAUSED"
    dialog.table.selectRow(0)
    assert [b.text() for b in dialog._action_buttons] == ["Continue my mission", "Cancel the mission"]
    dialog._action_buttons[0].click()
    assert sent == ["continue my mission"]
