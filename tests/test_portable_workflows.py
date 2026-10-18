from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.automation import portable
from app.automation.tasks import AutomationStore, Step
from app.automation.triggers import DailyTrigger, ManualTrigger
from app.config.settings import Settings
from app.tools import automation as tool
from app.tools.base import ToolContext, ToolError
from tests.test_automation import NOW, db, recording_registry  # noqa: F401


def world(db, tmp_path):  # noqa: F811
    registry = recording_registry([])
    ctx = ToolContext(Settings(), database=db, clock=lambda: NOW)
    ctx.paths = SimpleNamespace(data_dir=tmp_path, config_dir=tmp_path, env_file=tmp_path / ".env")
    ctx.get_tool_registry = lambda: registry
    ctx.path_policy = SimpleNamespace(resolve=lambda text, must_exist=False: Path(text))
    store = AutomationStore(db)
    trigger = DailyTrigger(time="08:00")
    task, _ = store.save("morning", trigger, [Step(type="tool", tool="step_one", arguments={"name": "a"}),
                                              Step(type="tool", tool="risky", arguments={"name": "b"}),
                                              Step(type="notify", message="done")], trigger.next_run(NOW))
    return registry, ctx, store, task


def doc_for(db, tmp_path):  # noqa: F811
    registry, ctx, store, task = world(db, tmp_path)
    return registry, ctx, portable.export_document(task, registry, ctx, "d", "o")


def test_export_declares_tools_and_risk(db, tmp_path) -> None:  # noqa: F811
    _, _, doc = doc_for(db, tmp_path)
    assert doc["format"] == "jarvis-workflow" and doc["requires"]["tools"] == ["step_one", "risky"]
    assert doc["requires"]["max_risk"] == "dangerous" and doc["requires"]["asks_for_approval"] == 1
    assert "trigger" not in doc and "08:00" not in json.dumps(doc)  # the schedule is not exported


def test_a_valid_file_validates(db, tmp_path) -> None:  # noqa: F811
    registry, ctx, doc = doc_for(db, tmp_path)
    report = portable.validate_import(json.dumps(doc), registry, ctx)
    assert report.ok and report.name == "morning" and report.max_risk == "dangerous"
    assert any("ask for your approval" in w for w in report.warnings)


def test_a_file_cannot_do_more_than_it_declares(db, tmp_path) -> None:  # noqa: F811
    registry, ctx, doc = doc_for(db, tmp_path)
    undeclared = copy.deepcopy(doc)
    undeclared["requires"]["tools"] = ["step_one"]
    assert "does not declare" in " ".join(portable.validate_import(json.dumps(undeclared), registry, ctx).problems)
    low = copy.deepcopy(doc)
    low["requires"]["max_risk"] = "safe"
    assert "above the declared maximum" in " ".join(portable.validate_import(json.dumps(low), registry, ctx).problems)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(format="other"), lambda d: d.update(schema=99), lambda d: d.update(extra="x"),
    lambda d: d.update(steps=[]), lambda d: d["steps"].__setitem__(0, {"type": "tool", "tool": "nope"}),
    lambda d: d["steps"].__setitem__(0, {"type": "tool", "tool": "schedule_task"}),
    lambda d: d.update(name="bad/name!!"), lambda d: d.__setitem__("requires", {"tools": [], "max_risk": "x"}),
    lambda d: d["steps"][0].update(arguments={"unknown_arg": {"x": 1}}),
])
def test_bad_files_are_refused(db, tmp_path, mutate) -> None:  # noqa: F811
    registry, ctx, doc = doc_for(db, tmp_path)
    bad = copy.deepcopy(doc)
    mutate(bad)
    assert not portable.validate_import(json.dumps(bad), registry, ctx).ok


def test_not_json_and_too_big(db, tmp_path) -> None:  # noqa: F811
    registry, ctx, _ = doc_for(db, tmp_path)
    assert "not JSON" in portable.validate_import("{nope", registry, ctx).problems[0]
    assert "larger" in portable.validate_import(" " * 70_000, registry, ctx).problems[0]


def test_tools_export_then_import_as_a_manual_unrun_workflow(db, tmp_path) -> None:  # noqa: F811
    registry, ctx, store, task = world(db, tmp_path)
    result = tool.export_workflow(tool.ExportArgs(name="morning"), ctx)
    path = result.data["path"]
    store.delete(task.id)
    checked = tool.import_workflow(tool.ImportArgs(path=path, check_only=True), ctx)
    assert checked.data["exists"] is False and store.all() == []
    done = tool.import_workflow(tool.ImportArgs(path=path), ctx)
    imported = store.find("morning")
    assert isinstance(imported.trigger, ManualTrigger) and imported.last_status is None and "not been run" in done.message
    with pytest.raises(ToolError, match="already exists"):
        tool.import_workflow(tool.ImportArgs(path=path), ctx)
    assert tool.import_workflow(tool.ImportArgs(path=path, replace=True), ctx).message.startswith("Replaced")
    assert "changed" in {v["reason"] for v in store.revisions("morning")}  # the replaced one is kept


def test_a_bad_file_fails_the_tool_check(db, tmp_path) -> None:  # noqa: F811
    registry, ctx, _, _ = world(db, tmp_path)
    bad = tmp_path / "x.json"
    bad.write_text('{"format": "jarvis-workflow", "schema": 1}')
    with pytest.raises(ToolError):
        tool._import_precheck(tool.ImportArgs(path=str(bad)), ctx)


def test_automations_cannot_import_workflows(db, tmp_path) -> None:  # noqa: F811
    from app.automation.tasks import FORBIDDEN_STEP_TOOLS

    assert {"import_workflow", "export_workflow"} <= FORBIDDEN_STEP_TOOLS
