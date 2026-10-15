"""3.4.1: the controller split into parts, settings groups, the verification contract, universal undo,
Mission Control."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import runs, undo
from app.memory.database import Database
from app.security.permissions import PermissionEngine
from app.tools.base import ToolContext, ToolResult
from app.tools.catalog import build_default_registry
from tests.test_filesystem import home, policy_for  # noqa: F401 (fixture)
from tests.test_tools import ScriptedConfirmer


# ------------------------------------------------------------------ structure


def test_controller_is_split_and_whole() -> None:
    from app.ui.controller import AssistantController

    lines = len(Path("app/ui/controller.py").read_text(encoding="utf-8").splitlines())
    assert lines < 800  # was 1,998
    for method in ("start_remote", "health_report", "start_hands_free", "check_alerts", "make_jarvis_model",
                   "send_text", "check_state", "pairing_code", "ask_by_voice"):
        assert callable(getattr(AssistantController, method)), method


def test_every_setting_is_in_exactly_one_group() -> None:
    from app.config.groups import GROUPS, group_of, grouped

    fields = list(Settings.model_fields)
    placed = [f for fields_ in GROUPS.values() for f in fields_]
    assert sorted(set(placed)) == sorted(placed), "a setting is in two groups"
    missing = [f for f in fields if group_of(f) is None]
    assert not missing, f"choose a group in app/config/groups.py for: {missing}"
    assert not set(placed) - set(fields), "groups.py names a setting that doesn't exist"
    assert "ai" in grouped(Settings().model_dump())


# ------------------------------------------------------------------ undo and verification


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "jarvis.db")
    database.initialize()
    return database


def executor(home: Path, db: Database, confirm: bool = True):
    ctx = ToolContext(Settings(), path_policy=policy_for(home), database=db)
    confirmer = ScriptedConfirmer(confirm)
    return ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer), \
        confirmer


def call(ex: ToolExecutor, name: str, **arguments):
    return asyncio.run(ex.execute(ToolCall(name, arguments))).result


def test_move_then_undo_moves_it_back(home: Path, db) -> None:
    ex, _ = executor(home, db)
    moved = call(ex, "move_item", source="report.pdf", destination="Documents")
    assert moved.ok and moved.data["verified"] is True and (home / "Documents" / "report.pdf").exists()
    assert "move report.pdf back to Downloads" in call(ex, "list_undoable").message
    undone = call(ex, "undo_last_action")
    assert undone.ok and (home / "Downloads" / "report.pdf").exists() and not (home / "Documents" / "report.pdf").exists()
    assert not call(ex, "undo_last_action").ok  # undoing isn't itself undoable; nothing left
    assert undo.UndoStore(db).pending() == []


def test_rename_and_create_can_be_undone(home: Path, db) -> None:
    ex, confirmer = executor(home, db)
    assert call(ex, "rename_item", path="notes.txt", new_name="ideas.txt").ok
    assert call(ex, "undo_last_action").ok and (home / "Documents" / "notes.txt").exists()
    assert call(ex, "create_item", path="Desktop\\Plans", kind="folder").ok
    result = call(ex, "undo_last_action")  # the new folder goes to the Recycle Bin - which still asks first
    assert result.ok and not (home / "Desktop" / "Plans").exists()
    assert confirmer.requests and confirmer.requests[-1].tool == "delete_item"


def test_undo_obeys_a_no(home: Path, db) -> None:
    ex, _ = executor(home, db, confirm=True)
    call(ex, "create_item", path="Desktop\\Keep", kind="folder")
    ex.confirmer = ScriptedConfirmer(False)
    result = call(ex, "undo_last_action")
    assert not result.ok and (home / "Desktop" / "Keep").exists() and undo.UndoStore(db).pending()


def test_volume_can_be_undone(db) -> None:
    inverse = undo.inverse_of(ToolCall("volume_control", {"action": "set", "level": 80}),
                              ToolResult.success("Volume set to 80%.", volume=80, previous=35))
    assert inverse.call == ToolCall("volume_control", {"action": "set", "level": 35})
    assert undo.inverse_of(ToolCall("open_application", {"name": "chrome"}), ToolResult.success("ok")) is None
    assert undo.inverse_of(ToolCall("move_item", {"source": "a"}), ToolResult.failure("x", "no")) is None


def test_a_failed_check_is_never_reported_as_success(home: Path, db, monkeypatch) -> None:
    import shutil

    from app.tools import filesystem

    monkeypatch.setattr(filesystem.shutil, "move", lambda a, b: None)  # "moved", but nothing happened
    ex, _ = executor(home, db)
    result = call(ex, "move_item", source="report.pdf", destination="Documents")
    assert not result.ok and result.error == "not_verified" and "when I checked" in result.message
    assert undo.UndoStore(db).pending() == []  # nothing to undo when it didn't happen
    assert shutil.move  # the real one is untouched outside the tool module


def test_undo_records_carry_the_run(home: Path, db) -> None:
    ex, _ = executor(home, db)
    with runs.running(runs.Run("voice", "move my report")) as run:
        call(ex, "move_item", source="report.pdf", destination="Documents")
    assert undo.UndoStore(db).pending()[0]["run_id"] == run.id


@pytest.mark.parametrize("said, tool", [("undo that", "undo_last_action"), ("undo your last action",
                                         "undo_last_action"), ("take that back", "undo_last_action"),
                                        ("what can you undo", "list_undoable"), ("open mission control",
                                                                                 "jarvis_control")])
def test_undo_commands(said, tool) -> None:
    from app.agent.shortcuts import match_shortcut

    found = match_shortcut(said, lambda n: None)
    assert found is not None and found.name == tool
    assert match_shortcut("undo", lambda n: None).name == "app_command"  # bare "undo" is still Ctrl+Z in your app


# ------------------------------------------------------------------ Mission Control


def test_mission_control_shows_runs_traces_and_undo(home: Path, db) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.memory.audit import AuditLog
    from app.ui.mission_control import MissionControlDialog, trace_rows

    ex, _ = executor(home, db)
    ex.audit = AuditLog(db)
    store = runs.RunStore(db)
    with runs.running(runs.Run("text", "move report.pdf to documents"), store):
        call(ex, "move_item", source="report.pdf", destination="Documents")
    undone = []
    dialog = MissionControlDialog(db, on_undo=lambda: undone.append(1))
    assert dialog.run_table.rowCount() == 1 and dialog.run_table.item(0, 1).text() == "text"
    assert dialog.trace_table.rowCount() >= 2 and dialog.undo_list.count() == 1
    rows = trace_rows(store.trace(store.recent(1)[0]["id"]))
    assert any(r[1] == "verification" for r in rows) and any(r[1] == "tool_result" and r[5].startswith("ok") for r in rows)
    dialog._undo()
    assert undone == [1]
