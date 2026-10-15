"""The My day window: today's overview, the to-do list and mission history (offscreen Qt)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.agent.shortcuts import match_shortcut  # noqa: E402
from app.config.settings import Settings  # noqa: E402
from app.core.missions import MissionStore, run, validate_plan  # noqa: E402
from app.tools import productivity as p  # noqa: E402
from app.tools.base import ToolContext  # noqa: E402
from tests.test_v2 import db  # noqa: E402
from tests.test_v3210 import make, step  # noqa: E402

NOW = datetime(2026, 9, 24, 9, 0)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def ctx_for(tmp_path: Path) -> ToolContext:
    return ToolContext(Settings(), database=db(tmp_path), clock=lambda: NOW)


def test_window_lists_todos_and_enables_buttons_only_with_a_selection(tmp_path: Path) -> None:
    from app.ui.my_day import MyDayDialog

    ctx = ctx_for(tmp_path)
    dialog = MyDayDialog(ctx)
    assert dialog.table.isHidden() and not dialog.empty_label.isHidden()  # empty state
    p.add_todo(p.TodoArgs(text="old thing", due="2026-09-20", priority="high"), ctx)
    p.add_todo(p.TodoArgs(text="later thing", after="old thing"), ctx)
    dialog.refresh()
    assert dialog.table.rowCount() == 2 and dialog.table.item(0, 0).text() == "High"
    assert dialog.table.item(1, 5).text() == "old thing"  # waiting for
    assert not any(b.isEnabled() for b in dialog.row_buttons)
    dialog.table.selectRow(0)
    assert all(b.isEnabled() for b in dialog.row_buttons)
    assert "Overdue" in dialog.today_text.toPlainText() and "old thing" in dialog.today_text.toPlainText()


def test_buttons_change_the_real_list(tmp_path: Path, monkeypatch) -> None:
    from app.ui.my_day import MyDayDialog

    ctx = ctx_for(tmp_path)
    p.add_todo(p.TodoArgs(text="write report", due="today"), ctx)
    p.add_todo(p.TodoArgs(text="send report", after="write report"), ctx)
    dialog = MyDayDialog(ctx)
    dialog.table.selectRow(0)
    dialog.cycle_priority()  # normal -> low
    assert p.open_todos(ctx)[0]["priority"] == 3
    dialog.table.selectRow(0)
    dialog.postpone_selected()
    assert p.open_todos(ctx)[0]["postponed"] == 1 and "Moved" in dialog.status.text()
    dialog.table.selectRow(0)
    dialog.complete_selected()
    assert [i["text"] for i in p.open_todos(ctx)] == ["send report"] and "Done: write report" in dialog.status.text()
    dialog.table.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    dialog.delete_selected()
    assert len(p.open_todos(ctx)) == 1  # said no: still there
    dialog.table.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog.delete_selected()
    assert p.open_todos(ctx) == []


def test_the_action_uses_the_item_id_even_if_the_list_changed(tmp_path: Path) -> None:
    from app.ui.my_day import MyDayDialog

    ctx = ctx_for(tmp_path)
    p.add_todo(p.TodoArgs(text="b item", due="tomorrow"), ctx)
    p.add_todo(p.TodoArgs(text="a item", due="today"), ctx)
    dialog = MyDayDialog(ctx)
    dialog.table.selectRow(0)  # "a item" (due today, first)
    p.add_todo(p.TodoArgs(text="urgent", due="2026-09-01"), ctx)  # the list reorders behind the window's back
    dialog.complete_selected()
    assert sorted(i["text"] for i in p.open_todos(ctx)) == ["b item", "urgent"]  # the one selected, not a shifted row


def test_add_dialog_builds_a_valid_item_and_bad_input_is_explained(tmp_path: Path, monkeypatch) -> None:
    from app.ui import my_day
    from app.ui.my_day import MyDayDialog

    ctx = ctx_for(tmp_path)
    dialog = MyDayDialog(ctx)

    def fake_exec(values):
        def run_(self):
            self.text.setText(values["text"])
            self.due.setText(values["due"])
            self.priority.setCurrentText(values["priority"])
            self.repeat.setCurrentText(values["repeat"])
            return my_day.QDialog.DialogCode.Accepted
        return run_

    monkeypatch.setattr(my_day.AddTodoDialog, "exec", fake_exec(
        {"text": "water plants", "due": "today", "priority": "high", "repeat": "weekly"}))
    dialog.add_todo()
    [item] = p.open_todos(ctx)
    assert item["repeat"] == "weekly" and item["priority"] == 1
    monkeypatch.setattr(my_day.AddTodoDialog, "exec", fake_exec(
        {"text": "no date", "due": "", "priority": "normal", "repeat": "daily"}))
    dialog.add_todo()
    assert "needs a due day" in dialog.status.text() and len(p.open_todos(ctx)) == 1


def test_history_tab_shows_the_missions_timeline(tmp_path: Path) -> None:
    from app.ui.my_day import MyDayDialog

    database = db(tmp_path)
    ex, _, _ = make(database)
    store = MissionStore(database)
    mission = store.create("ship it", validate_plan([step("look", "a"), step("look", "b")], ex.registry, ex.context))
    asyncio.run(run(mission, store, ex, approved=True))
    dialog = MyDayDialog(ToolContext(Settings(), database=database, clock=lambda: NOW))
    assert dialog.mission_list.count() == 1 and "completed" in dialog.mission_list.item(0).text()
    text = dialog.history.toPlainText()
    assert "planned -> running" in text and "running -> verifying" in text and "verifying -> completed" in text


def test_voice_phrases_open_the_window_or_answer(tmp_path: Path) -> None:
    none = lambda name: None  # noqa: E731
    assert match_shortcut("show my day", none).arguments == {"action": "my_day"}
    assert match_shortcut("how is my day going", none).name == "command_center"
    call = match_shortcut("what did I complete today", none)
    assert call.name == "command_center" and call.arguments == {"evening": True}


def test_task_center_colours_the_states(tmp_path: Path) -> None:
    from app.ui.task_center import STATUS_COLOR, TaskCenterDialog
    from app.core import task_center

    assert set(STATUS_COLOR) == set(task_center.STATUSES)
    database = db(tmp_path)
    ex, _, _ = make(database)
    MissionStore(database).create("x1", validate_plan([step("breaks", "a")], ex.registry, ex.context))
    dialog = TaskCenterDialog(database, lambda text: None)
    assert dialog.table.item(0, 0).foreground().color().name() == STATUS_COLOR["QUEUED"]
