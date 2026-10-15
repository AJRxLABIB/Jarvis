from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.automation import filewatch
from app.automation.tasks import Step
from app.automation.triggers import EventTrigger, ManualTrigger
from app.tools.base import ToolError
from tests.test_automation import NOW, Clock, db, scheduler_for, steps  # noqa: F401


def test_snapshot_and_changed(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_text("1")
    (tmp_path / "b.txt").write_text("1")
    value = f"{tmp_path}|*.pdf"
    first = filewatch.snapshot(value)
    assert set(first) == {"a.pdf"}
    assert filewatch.changed(None, first) == []  # first look is only a baseline
    assert filewatch.changed(first, filewatch.snapshot(value)) == []
    (tmp_path / "c.PDF").write_text("2")
    (tmp_path / "a.pdf").write_text("changed!")
    os.utime(tmp_path / "a.pdf", ns=(1, 1))
    assert filewatch.changed(first, filewatch.snapshot(value)) == ["a.pdf", "c.PDF"]


def test_unreadable_folder_is_not_a_change(tmp_path: Path) -> None:
    first = filewatch.snapshot(str(tmp_path))
    assert filewatch.snapshot(str(tmp_path / "gone")) is None
    assert filewatch.changed(first, None) == []


@pytest.mark.parametrize("bad", ["", "relative/folder", "C:\\definitely\\missing\\folder"])
def test_validate_watch_rejects(bad: str) -> None:
    with pytest.raises(ToolError):
        filewatch.validate_watch(bad)


def test_pattern_cannot_be_a_path(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        filewatch.validate_watch(f"{tmp_path}|../*.pdf")


def test_file_trigger_runs_once_per_new_file(db, tmp_path: Path) -> None:  # noqa: F811
    calls, clock = [], Clock(NOW)
    scheduler, _, _ = scheduler_for(db, calls, clock)
    trigger = EventTrigger(event="file_changed", value=f"{tmp_path}|*.pdf")
    scheduler.store.save("inbox", trigger, steps("step_one"), None)
    assert asyncio.run(scheduler.tick()) == [] and calls == []  # baseline
    assert asyncio.run(scheduler.tick()) == []  # nothing new
    (tmp_path / "new.pdf").write_text("x")
    (tmp_path / "ignored.txt").write_text("x")
    assert len(asyncio.run(scheduler.tick())) == 1 and len(calls) == 1
    assert asyncio.run(scheduler.tick()) == []  # the same file doesn't fire again
    assert "new.pdf" in trigger.describe() or "*.pdf" in trigger.describe()


def test_conditions_skip_steps(db) -> None:  # noqa: F811
    calls, clock = [], Clock(NOW)  # NOW is a Wednesday
    scheduler, _, _ = scheduler_for(db, calls, clock, fail_on={"step_one"}, battery=lambda: (50.0, True))
    flow = [
        Step(type="tool", tool="step_one", arguments={}, continue_on_error=True),
        Step(type="tool", tool="step_two", arguments={}, when="previous_ok"),
        Step(type="tool", tool="step_three", arguments={}, when="previous_failed"),
        Step(type="notify", message="weekend only", when="weekend"),
        Step(type="tool", tool="step_two", arguments={}, when="on_battery"),
    ]
    task, _ = scheduler.store.save("cond", ManualTrigger(), flow, None)
    report = asyncio.run(scheduler.run(task, manual=True))
    assert report.ok
    assert [c[0] for c in calls] == ["step_one", "step_three"]
    assert sum(1 for line in report.steps if line.startswith("skipped")) == 3


def test_condition_describes_itself() -> None:
    assert "only if previous ok" in Step(type="tool", tool="x", when="previous_ok").describe()


def test_a_failed_workflow_resumes_at_the_failed_step(db) -> None:  # noqa: F811
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock, fail_on={"step_two"})
    task, _ = scheduler.store.save("chain", ManualTrigger(), steps("step_one", "step_two", "step_three"), None)
    first = asyncio.run(scheduler.run(task, manual=True))
    assert not first.ok and "resume chain" in first.message and "from step 2" in first.message
    assert scheduler.store.progress(task.id) == 2 and [c[0] for c in calls] == ["step_one", "step_two"]
    scheduler2, _, _ = scheduler_for(db, calls, clock)  # the problem is fixed
    calls.clear()
    again = asyncio.run(scheduler2.run(task, manual=True, resume=True))
    assert again.ok and [c[0] for c in calls] == ["step_two", "step_three"]  # step one is NOT repeated
    assert again.steps[0].startswith("resumed at step 2") and scheduler2.store.progress(task.id) is None


def test_a_normal_run_starts_over_and_a_stale_checkpoint_is_ignored(db) -> None:  # noqa: F811
    calls, clock = [], Clock(NOW)
    scheduler, _, _ = scheduler_for(db, calls, clock)
    task, _ = scheduler.store.save("chain", ManualTrigger(), steps("step_one", "step_two"), None)
    scheduler.store.set_progress(task.id, 2)
    assert asyncio.run(scheduler.run(task, manual=True)).ok and [c[0] for c in calls] == ["step_one", "step_two"]
    scheduler.store.set_progress(task.id, 99)  # more than the steps now saved
    calls.clear()
    assert asyncio.run(scheduler.run(task, manual=True, resume=True)).ok and len(calls) == 2


def test_deleting_a_workflow_forgets_its_checkpoint(db) -> None:  # noqa: F811
    scheduler, _, _ = scheduler_for(db, [], Clock(NOW))
    task, _ = scheduler.store.save("gone", ManualTrigger(), steps("step_one"), None)
    scheduler.store.set_progress(task.id, 1)
    scheduler.store.delete(task.id)
    assert scheduler.store.progress(task.id) is None


def test_resume_phrase_matches_only_known_workflows() -> None:
    from app.agent.shortcuts import match_shortcut

    known = lambda name: "morning setup" if name.lower() == "morning setup" else None  # noqa: E731
    call = match_shortcut("resume morning setup", lambda n: None, known_workflow=known)
    assert call is not None and call.name == "run_workflow" and call.arguments == {"name": "morning setup", "resume": True}
    assert match_shortcut("resume the unknown thing", lambda n: None, known_workflow=known) is None
