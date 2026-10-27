"""Goal contracts verify mission completion; saved workflows keep a reliability history and are suspended when broken."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from app.automation import reliability as R
from app.automation.tasks import AutomationStore
from app.automation.triggers import ManualTrigger
from app.core import goal_contract as gc
from app.core import missions
from app.core.missions import MissionStore
from tests.test_automation import NOW, Clock, db, scheduler_for, steps  # noqa: F401  (pytest fixture + helpers)
from tests.test_v3210 import db as mdb  # noqa: F401
from tests.test_v3210 import go, make, step


# ------------------------------------------------------------------ goal check at mission completion


def _fake_step(n, tool, state, **args):
    return NS(n=n, tool=tool, state=state, arguments=args)


def test_check_goal_flags_contradiction_and_missing_confirmation():
    m = NS(title="open notepad", steps=[_fake_step(1, "open_application", "failed", name="notepad")])
    verdict, note = missions._check_goal(m)
    assert verdict == "contradicted" and "notepad" in note.lower()
    m = NS(title="open notepad", steps=[_fake_step(1, "list_dir", "done")])
    verdict, note = missions._check_goal(m)
    assert verdict == "incomplete" and "could not confirm" in note
    m = NS(title="open notepad", steps=[_fake_step(1, "open_application", "done", name="notepad")])
    assert missions._check_goal(m) == ("complete", "")


def test_check_goal_never_raises_and_unverifiable_goal_is_stated(monkeypatch):
    assert missions._check_goal(NS(title="think about things", steps=[]))[0] == "unverifiable"
    monkeypatch.setattr(gc, "compile", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert missions._check_goal(NS(title="x", steps=[])) == ("error", "")


def test_mission_completes_with_an_honest_note_when_the_goal_could_not_be_confirmed(mdb):
    ex, _, _ = make(mdb)
    assert go(ex, "start_mission", title="open notepad", steps=[step("look", "a")]).ok
    mission = MissionStore(mdb).recent(1)[0]
    assert mission.state == "completed" and "could not confirm" in mission.note


def test_contradicted_goal_pauses_instead_of_completing(mdb, monkeypatch):
    ex, _, _ = make(mdb)
    monkeypatch.setattr(missions, "_check_goal", lambda m: ("contradicted", "notepad did not open"))
    result = go(ex, "start_mission", title="open notepad", steps=[step("look", "a")])
    assert not result.ok and "goal is not met" in result.message
    assert MissionStore(mdb).recent(1)[0].state == "paused"


def test_plan_that_breaks_what_you_said_never_starts(mdb):
    ex, _, log = make(mdb)
    result = go(ex, "start_mission", title="tidy my folder but don't delete anything", steps=[step("look", "a"), {
        "title": "remove it", "tool": "delete_item", "arguments": {"path": "x"}}])
    assert not result.ok and result.error == "goal_conflict" and log == []


# ------------------------------------------------------------------ skill reliability


def test_history_counts_only_the_skills_own_failures(db):
    R.record(db, "morning", True)
    R.record(db, "morning", False, 2, "stopped at step 2 (open_application): not found")
    R.record(db, "morning", False, 1, "'morning' was stopped before step 1 by the emergency controls.")   # not counted
    R.record(db, "morning", False, None, "declined by you")                                                # not counted
    s = R.stats(db, "morning")
    assert (s.runs, s.ok, s.streak_failures) == (2, 1, 1) and "not found" in s.last_failure
    assert "50%" in s.describe()
    R.record(db, "morning", True)
    assert R.stats(db, "morning").streak_failures == 0


def test_history_is_bounded(db):
    for _ in range(R.KEEP_ROWS + 20):
        R.record(db, "loop", True)
    assert R.stats(db, "loop").runs == R.KEEP_ROWS


def test_three_failures_in_a_row_suspend_the_skill_with_a_reason_and_reenabling_resets(db):
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock, fail_on={"step_two"})
    task, _ = scheduler.store.save("broken routine", ManualTrigger(), steps("step_one", "step_two"), None)
    for i in range(R.SUSPEND_AFTER):
        report = asyncio.run(scheduler.run(scheduler.store.get(task.id), manual=True))
        assert not report.ok
        assert scheduler.store.get(task.id).enabled == (i < R.SUSPEND_AFTER - 1)
    after = scheduler.store.get(task.id)
    assert not after.enabled and "suspended" in (after.last_status or "") and "3 runs in a row" in after.last_status
    assert any("suspended" in m for _, m in notes)
    assert scheduler.store.get(task.id) is not None                      # suspended, never deleted
    scheduler.store.set_enabled(task.id, True, None)                     # the user fixed it and switched it on
    assert scheduler.store.get(task.id).enabled and R.stats(db, "broken routine").runs == 0


def test_a_success_between_failures_prevents_suspension(db):
    calls, clock = [], Clock(NOW)
    scheduler, _, _ = scheduler_for(db, calls, clock, fail_on={"step_two"})
    task, _ = scheduler.store.save("flaky", ManualTrigger(), steps("step_one", "step_two"), None)
    for _ in range(2):
        asyncio.run(scheduler.run(scheduler.store.get(task.id), manual=True))
    R.record(db, "flaky", True)
    asyncio.run(scheduler.run(scheduler.store.get(task.id), manual=True))
    assert scheduler.store.get(task.id).enabled


def test_listing_shows_reliability(db):
    from app.tools.automation import list_automations
    from app.tools.base import NoArgs, ToolContext
    from app.config.settings import Settings

    store = AutomationStore(db)
    store.save("shown", ManualTrigger(), steps("step_one"), None)
    R.record(db, "shown", True)
    R.record(db, "shown", False, 1, "boom")
    result = list_automations(NoArgs(), ToolContext(Settings(), database=db))
    row = next(a for a in result.data["automations"] if a["name"] == "shown")
    assert "1 of 2" in row["reliability"]
