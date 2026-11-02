from __future__ import annotations

from app.automation import reliability
from app.automation import skill_card as S
from app.automation.tasks import AutomationStore, Step
from app.automation.triggers import ManualTrigger
from app.ai.base import ToolCall
from app.core.undo import inverse_of
from app.tools.base import ToolResult
from tests.test_automation import NOW, db, recording_registry  # noqa: F401


def make(db):  # noqa: F811
    registry = recording_registry([])
    store = AutomationStore(db)
    flow = [Step(type="tool", tool="step_one", arguments={"name": "a"}),
            Step(type="tool", tool="risky", arguments={"name": "b"}, when="weekday", continue_on_error=True),
            Step(type="notify", message="done")]
    task, _ = store.save("morning", ManualTrigger(), flow, None, parameters=None)
    return registry, store, task


def test_card_reports_risk_failure_handling_and_what_is_not_recorded(db):  # noqa: F811
    registry, store, task = make(db)
    card = S.build(task, registry, db)
    assert [s.tool for s in card.steps] == ["step_one", "risky", "notify"]
    risky = card.steps[1]
    assert risky.asks and risky.on_failure == "continues" and risky.condition == "only if weekday" and not risky.undoable
    assert card.steps[0].on_failure == "stops here" and card.highest_risk in ("moderate", "dangerous")
    text = card.render()
    assert "cannot be undone automatically" in text and "Not recorded:" in text and "expected result" in text and "has not run yet" in text


def test_card_history_and_version_come_from_the_real_stores(db):  # noqa: F811
    registry, store, task = make(db)
    reliability.record(db, "morning", True)
    reliability.record(db, "morning", False, 2, "step 2 failed: disk full")
    reliability.record(db, "morning", False, 2, "step 2 failed: disk full")
    store.save("morning", ManualTrigger(), [Step(type="notify", message="v2")], None)
    card = S.build(store.get(task.id), registry, db)
    assert card.version == 2 and (card.runs, card.succeeded, card.failing_streak) == (3, 1, 2)
    assert "the last 2 failed (step 2 failed: disk full)" in card.render()


def test_unknown_tool_is_reported_not_hidden(db):  # noqa: F811
    registry, store, _ = make(db)
    gone, _ = store.save("old", ManualTrigger(), [Step(type="tool", tool="removed_tool")], None)
    card = S.build(gone, registry, db)
    assert card.steps[0].risk == "unknown tool" and card.highest_risk == "blocked"


def test_undoable_list_matches_the_undo_module():
    samples = {"move_item": ({"source": "a", "destination": "b"}, {"path": "b/a", "old_path": "a"}), "rename_item": ({}, {"path": "x", "old_path": "y"}),
               "create_item": ({}, {"path": "p"}), "volume_control": ({"action": "set"}, {"previous": 20}), "brightness_control": ({"action": "up"}, {"previous": 50}),
               "edit_code": ({}, {"path": "f", "backup": "b"}), "zip_items": ({}, {"path": "z"})}
    for tool, (args, data) in samples.items():
        assert tool in S.UNDOABLE_TOOLS and inverse_of(ToolCall(tool, args), ToolResult.success("ok", **data)) is not None
    assert "run_command" not in S.UNDOABLE_TOOLS
