from __future__ import annotations

from datetime import datetime

import pytest

from app.agent.shortcuts import match_shortcut
from app.automation import studio
from app.automation.tasks import AutomationStore, Step
from app.automation.triggers import DailyTrigger, ManualTrigger
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.security.permissions import PermissionEngine
from app.tools.base import ToolContext, ToolError
from tests.test_automation import NOW, db, recording_registry  # noqa: F401


def setup(db, tmp_path=None):  # noqa: F811
    registry = recording_registry([])
    ctx = ToolContext(Settings(), database=db, clock=lambda: NOW)
    store = AutomationStore(db)
    flow = [Step(type="tool", tool="step_one", arguments={"name": "a"}),
            Step(type="tool", tool="risky", arguments={"name": "b"}, when="weekday"),
            Step(type="notify", message="done")]
    task, _ = store.save("morning", ManualTrigger(), flow, None)
    return registry, ctx, store, task


def test_dry_run_reports_risk_conditions_and_runs_nothing(db) -> None:  # noqa: F811
    registry, ctx, store, task = setup(db)
    perms = PermissionEngine(PermissionsConfig())
    rows = studio.dry_run(task, registry, ctx, perms, weekday=False, plugged=None)
    assert [r.risk for r in rows] == ["safe", "dangerous", "safe"]
    assert rows[1].asks and rows[1].would_run == "skipped (condition)" and rows[0].would_run == "yes"
    text = studio.describe(rows)
    assert "asks first" in text and "only if weekday" in text and "unattended run stops" in text
    assert store.get(task.id).last_status is None  # nothing ran


def test_dry_run_flags_a_tool_that_no_longer_exists(db) -> None:  # noqa: F811
    registry, ctx, store, _ = setup(db)
    gone, _ = store.save("old", ManualTrigger(), [Step(type="tool", tool="removed_tool")], None)
    row = studio.dry_run(gone, registry, ctx, PermissionEngine(PermissionsConfig()), weekday=True, plugged=True)[0]
    assert "no tool named" in row.problem


def test_edit_steps_one_change_at_a_time(db) -> None:  # noqa: F811
    _, _, _, task = setup(db)
    up = studio.edit_steps(task.steps, "up", 1)
    assert up[0].tool == "risky" and task.steps[0].tool == "step_one"  # the original list is untouched
    assert studio.edit_steps(task.steps, "down", 0)[1].tool == "step_one"
    assert len(studio.edit_steps(task.steps, "remove", 2)) == 2
    assert studio.edit_steps(task.steps, "condition", 0, "on_battery")[0].when == "on_battery"
    assert studio.edit_steps(task.steps, "condition", 1, "")[1].when is None
    assert studio.edit_steps(task.steps, "continue_on_error", 0)[0].continue_on_error
    added = studio.edit_steps(task.steps, "add_note", 0, "halfway")
    assert added[1].type == "notify" and added[1].message == "halfway"
    for bad in (("up", 9), ("sideways", 0), ("condition", 0, "never")):
        with pytest.raises(ToolError):
            studio.edit_steps(task.steps, *bad)
    with pytest.raises(ToolError, match="at least one"):
        studio.edit_steps(task.steps[:1], "remove", 0)


def test_save_validates_keeps_the_trigger_and_the_old_version(db) -> None:  # noqa: F811
    registry, ctx, store, _ = setup(db)
    trigger = DailyTrigger(time="08:00")
    daily, _ = store.save("daily", trigger, [Step(type="tool", tool="step_one", arguments={"name": "a"})],
                          trigger.next_run(NOW))
    store.set_enabled(daily.id, False, None)
    paused = store.get(daily.id)
    saved = studio.save(store, paused, [*paused.steps, Step(type="notify", message="hi")], registry, ctx)
    assert len(saved.steps) == 2 and saved.trigger.time == "08:00" and not saved.enabled  # still paused
    assert len(studio.previous_versions(store, "daily")) == 1
    with pytest.raises(ToolError):
        studio.save(store, saved, [Step(type="tool", tool="not_a_tool")], registry, ctx)
    assert len(store.get(saved.id).steps) == 2  # a rejected edit stored nothing


def test_open_phrase() -> None:
    call = match_shortcut("open workflow studio", lambda n: None)
    assert call is not None and call.arguments == {"action": "workflow_studio"}


# ---------------------------------------------------------------- the window

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_window_edits_and_saves(qapp, db) -> None:  # noqa: F811
    from app.ui.workflow_studio import WorkflowStudioDialog

    registry, ctx, store, task = setup(db)
    sent: list[str] = []
    dialog = WorkflowStudioDialog(ctx, sent.append, registry=registry, permissions=PermissionEngine(PermissionsConfig()))
    assert dialog.table.rowCount() == 3 and not dialog.buttons["save"].isEnabled()
    dialog.table.selectRow(1)
    dialog.buttons["up"].click()
    assert dialog.dirty and dialog.steps[0].tool == "risky" and dialog.buttons["save"].isEnabled()
    dialog.buttons["run"].click()
    assert sent == [] and "Save your changes first" in dialog.status.text()
    dialog.buttons["dry"].click()
    assert "nothing was run" in dialog.report.toPlainText().lower()
    dialog.buttons["save"].click()
    assert "Saved 'morning'" in dialog.status.text() and not dialog.dirty
    assert store.find("morning").steps[0].tool == "risky" and dialog.versions.count() == 1
    dialog.buttons["run"].click()
    dialog.buttons["resume"].click()
    assert sent == ["run morning", "resume morning"]
    dialog.buttons["restore"].setEnabled(True)


def test_window_simulates_a_what_if_on_the_unsaved_steps_and_runs_nothing(qapp, db) -> None:  # noqa: F811
    from app.ui.workflow_studio import WorkflowStudioDialog

    registry, ctx, store, task = setup(db)
    dialog = WorkflowStudioDialog(ctx, None, registry=registry, permissions=PermissionEngine(PermissionsConfig()))
    assert dialog.buttons["sim"].isEnabled()
    dialog.table.selectRow(0)
    dialog.simulate("tool_fails")
    text = dialog.report.toPlainText()
    assert "SIMULATED" in text and "Nothing real was run" in dialog.status.text()
    dialog.simulate("crash_and_restart")
    assert "SIMULATED" in dialog.report.toPlainText()
    assert store.get(task.id).last_status is None           # nothing was changed or run


def test_window_with_nothing_saved(qapp, db) -> None:  # noqa: F811
    from app.ui.workflow_studio import WorkflowStudioDialog

    ctx = ToolContext(Settings(), database=db, clock=lambda: NOW)
    dialog = WorkflowStudioDialog(ctx, None, registry=recording_registry([]),
                                  permissions=PermissionEngine(PermissionsConfig()))
    assert "Nothing is saved" in dialog.status.text()
    assert not dialog.buttons["dry"].isEnabled() and not dialog.buttons["up"].isEnabled()
    assert datetime.now().year >= 2026
