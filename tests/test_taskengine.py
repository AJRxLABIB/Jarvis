"""Task engine: enforced state transitions, verification before completion, restart safety, history."""

from __future__ import annotations

import asyncio

import pytest

from app.core import taskstate
from app.core.missions import MissionStore, run, validate_plan
from app.tools.base import ToolError
from tests.test_v3210 import db, go, make, step  # noqa: F401  (pytest fixture + helpers)


def test_transition_table() -> None:
    assert taskstate.can_transition("planned", "running")
    assert taskstate.can_transition("running", "verifying") and taskstate.can_transition("verifying", "completed")
    assert not taskstate.can_transition("running", "completed")  # must be verified first
    assert not taskstate.can_transition("completed", "running")
    assert not taskstate.can_transition("rolled_back", "running")
    assert taskstate.can_transition("completed", "completed")
    with pytest.raises(taskstate.InvalidTransition):
        taskstate.check_transition("planned", "bogus")


def test_store_refuses_invalid_transition_and_records_history(db) -> None:
    ex, _, _ = make(db)
    store = MissionStore(db)
    mission = store.create("t", validate_plan([step("look", "a")], ex.registry, ex.context))
    store.set_state(mission, "running", "go")
    with pytest.raises(ToolError) as caught:
        store.set_state(mission, "completed")
    assert caught.value.code == "invalid_transition" if hasattr(caught.value, "code") else True
    assert mission.state == "running"
    store.set_state(mission, "verifying")
    store.set_state(mission, "completed", "ok")
    assert [(e["old_state"], e["new_state"]) for e in store.history(mission.id)] == [
        ("planned", "running"), ("running", "verifying"), ("verifying", "completed")]


def test_completion_goes_through_verifying(db) -> None:
    ex, _, log = make(db)
    assert go(ex, "start_mission", title="job x", steps=[step("look", "a"), step("look", "b")]).ok
    mission = MissionStore(db).recent(1)[0]
    assert mission.state == "completed"
    assert [e["new_state"] for e in MissionStore(db).history(mission.id)] == ["running", "verifying", "completed"]


def test_verify_completion_reports_skipped_and_unfinished() -> None:
    class S:
        def __init__(self, n, state):
            self.n, self.title, self.state, self.result = n, f"s{n}", state, ""

    assert taskstate.verify_completion([S(1, "done"), S(2, "done")]) == (True, "")
    ok, note = taskstate.verify_completion([S(1, "done"), S(2, "skipped")])
    assert ok and "skipped" in note
    ok, note = taskstate.verify_completion([S(1, "done"), S(2, "pending")])
    assert not ok and "not finished" in note
    assert taskstate.verify_completion([S(1, "skipped")])[0] is False  # nothing actually ran


def test_interrupted_side_effect_step_is_not_repeated_blindly(db) -> None:
    ex, _, log = make(db)
    store = MissionStore(db)
    mission = store.create("job", validate_plan([step("create_item", "a"), step("look", "b")], ex.registry, ex.context))
    mission.steps[0].state, mission.steps[0].attempts = "running", 1  # cut off mid-step by a crash
    store.save_step(mission, mission.steps[0])
    store.set_state(mission, "running")
    store.mark_interrupted()
    resumed = store.get(mission.id)
    outcome = asyncio.run(run(resumed, store, ex, approved=True))
    assert not outcome.ok and "may already have happened" in outcome.message
    assert log == []  # create_item was NOT run again
    assert store.get(mission.id).state == "paused"
    # you decide: skip it (it did happen) and the rest continues
    again = store.get(mission.id)
    outcome = asyncio.run(run(again, store, ex, approved=True, skip_failed=True))
    assert outcome.ok and log == ["look:b"]


def test_interrupted_read_only_step_is_simply_rerun(db) -> None:
    ex, _, log = make(db)
    store = MissionStore(db)
    mission = store.create("job", validate_plan([step("look", "a")], ex.registry, ex.context))
    mission.steps[0].state, mission.steps[0].attempts = "running", 1
    store.save_step(mission, mission.steps[0])
    store.set_state(mission, "running")
    store.mark_interrupted()
    assert asyncio.run(run(store.get(mission.id), store, ex, approved=True)).ok and log == ["look:a"]


def test_finished_mission_is_not_run_again(db) -> None:
    ex, _, log = make(db)
    go(ex, "start_mission", title="job x", steps=[step("look", "a")])
    mission = MissionStore(db).recent(1)[0]
    outcome = asyncio.run(run(mission, MissionStore(db), ex, approved=True))
    assert outcome.ok and log == ["look:a"]  # still just the one run


# ------------------------------------------------------------------ scheduler: overlap and at-most-once

from datetime import timedelta  # noqa: E402

from app.automation.triggers import ManualTrigger, once_at  # noqa: E402
from tests.test_automation import NOW, Clock, scheduler_for, steps  # noqa: E402


def test_a_task_never_overlaps_itself(db) -> None:
    calls, clock = [], Clock(NOW)
    scheduler, _, _ = scheduler_for(db, calls, clock)
    task, _ = scheduler.store.save("job", ManualTrigger(), steps("step_one"), None)
    scheduler._running.add(task.id)  # a run is in progress
    report = asyncio.run(scheduler.run(task, manual=True))
    assert not report.ok and "already running" in report.message and calls == []
    scheduler._running.discard(task.id)
    assert asyncio.run(scheduler.run(task, manual=True)).ok and len(calls) == 1


def test_one_time_task_is_claimed_before_it_runs(db) -> None:
    calls, clock = [], Clock(NOW)
    scheduler, _, _ = scheduler_for(db, calls, clock)
    trigger = once_at(NOW, in_minutes=1)
    task, _ = scheduler.store.save("once", trigger, steps("step_one"), trigger.at)
    seen = {}
    original = scheduler.executor.execute

    async def spy(call, **kw):
        stored = scheduler.store.get(task.id)
        seen["enabled"], seen["status"] = stored.enabled, stored.last_status
        return await original(call, **kw)

    scheduler.executor.execute = spy
    clock.now = NOW + timedelta(minutes=2)
    asyncio.run(scheduler.tick())
    assert seen == {"enabled": False, "status": "running"}  # claimed first: a crash can't repeat it
    assert scheduler.store.get(task.id).last_status == "ok"


def test_crash_during_a_one_time_task_is_reported_not_repeated(db) -> None:
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock)
    trigger = once_at(NOW, in_minutes=1)
    task, _ = scheduler.store.save("once", trigger, steps("step_one"), trigger.at)
    scheduler.store.record_run(task.id, "running", None, disable=True)  # what a crash mid-run leaves behind
    clock.now = NOW + timedelta(minutes=2)
    asyncio.run(scheduler.catch_up())
    asyncio.run(scheduler.tick())
    assert calls == [] and notes and notes[0][0] == "Interrupted"
    assert scheduler.store.get(task.id).last_status.startswith("interrupted")


# ------------------------------------------------------------------ orchestrator: duplicates, budget, cancel

from app.core import orchestrator  # noqa: E402
from tests.test_v3230 import go as agent_go, make as agent_make  # noqa: E402,F401


def test_duplicate_state_changing_step_is_rejected() -> None:
    ex, _ = agent_make()
    plan = [{"agent": "organizer", "tool": "move_item", "arguments": {"name": "a"}},
            {"agent": "organizer", "tool": "move_item", "arguments": {"name": "a"}}]
    with pytest.raises(ToolError, match="repeats step 1"):
        orchestrator.validate_plan(plan, ex.registry)
    plan[1]["arguments"] = {"name": "b"}  # different target: fine
    assert len(orchestrator.validate_plan(plan, ex.registry)) == 2
    reads = [{"agent": "researcher", "tool": "web_search", "arguments": {"name": "q"}}] * 2
    assert len(orchestrator.validate_plan(reads, ex.registry)) == 2  # read-only repeats are harmless


def test_plan_time_budget_marks_unrun_steps() -> None:
    ex, log = agent_make()
    steps_ = orchestrator.validate_plan([{"agent": "researcher", "tool": "web_search", "arguments": {"name": "q"}}], ex.registry)
    outcome = asyncio.run(orchestrator.run_plan(ex, "g", steps_, approved=True, budget_seconds=-1))
    assert not outcome.ok and steps_[0].state == "not_run" and log == []
    assert "time budget" in outcome.message


# ------------------------------------------------------------------ reminders: snooze

from tests.test_automation import run_tool  # noqa: E402


def test_snooze_moves_the_same_reminder_and_it_survives_a_restart(db) -> None:
    clock = Clock(NOW)
    assert run_tool(db, "set_reminder", clock=clock, message="stretch", in_minutes=5)[0].result.ok
    clock.now = NOW + timedelta(minutes=6)
    scheduler, notes, _ = scheduler_for(db, [], clock)
    asyncio.run(scheduler.tick())
    assert notes == [("Reminder: stretch", "stretch")]  # it went off
    execution, _ = run_tool(db, "snooze_reminder", clock=clock, minutes=15)
    assert execution.result.ok and "again in 15 minute(s)" in execution.result.message
    reminders = [t for t in scheduler.store.all() if t.name.startswith("Reminder:")]
    assert len(reminders) == 1 and reminders[0].enabled  # moved, not duplicated
    # "restart": a brand new scheduler on the same database still has it and fires it on time
    restarted, notes2, _ = scheduler_for(db, [], clock)
    clock.now += timedelta(minutes=10)
    asyncio.run(restarted.tick())
    assert notes2 == []
    clock.now += timedelta(minutes=6)
    asyncio.run(restarted.tick())
    assert notes2 == [("Reminder: stretch", "stretch")]


def test_snooze_without_a_reminder_says_so(db) -> None:
    assert run_tool(db, "snooze_reminder")[0].result.error == "no_reminder"


# ------------------------------------------------------------------ idempotency within one request

from app.agent.tool_manager import ONCE_PER_RUN_TOOLS, ToolExecutor, ToolRegistry  # noqa: E402
from app.config.files import PermissionsConfig  # noqa: E402
from app.config.settings import Settings  # noqa: E402
from app.core import runs  # noqa: E402
from app.ai.base import ToolCall  # noqa: E402
from app.security.permissions import PermissionEngine, RiskLevel  # noqa: E402
from app.tools.base import Tool, ToolContext, ToolResult  # noqa: E402
from tests.test_v3210 import NameArgs  # noqa: E402


def _sender():
    sent: list[str] = []

    def send(args, ctx):
        sent.append(args.name)
        return ToolResult.success(f"sent to {args.name}")

    tools = [Tool("send_email", "send", send, NameArgs, RiskLevel.SAFE),
             Tool("set_volume", "volume", send, NameArgs, RiskLevel.SAFE)]
    ex = ToolExecutor(ToolRegistry(tools), PermissionEngine(PermissionsConfig()), ToolContext(Settings()))
    return ex, sent


def test_a_repeated_one_shot_call_in_one_request_runs_once() -> None:
    ex, sent = _sender()

    async def go():
        with runs.running(runs.Run("text", "email Rafi")):
            first = await ex.execute(ToolCall("send_email", {"name": "rafi"}))
            again = await ex.execute(ToolCall("send_email", {"name": "rafi"}))  # a retry / the model repeating itself
            other = await ex.execute(ToolCall("send_email", {"name": "mina"}))
        return first.result, again.result, other.result

    first, again, other = asyncio.run(go())
    assert sent == ["rafi", "mina"]
    assert first.ok and again.ok and again.data.get("duplicate") and "did not do it again" in again.message
    assert other.ok and not other.data.get("duplicate")


def test_a_new_request_may_repeat_it_and_normal_repeats_are_untouched() -> None:
    ex, sent = _sender()

    async def go():
        for _ in range(2):  # two separate requests
            with runs.running(runs.Run("text", "email Rafi")):
                await ex.execute(ToolCall("send_email", {"name": "rafi"}))
        with runs.running(runs.Run("text", "volume")):
            await ex.execute(ToolCall("set_volume", {"name": "up"}))
            await ex.execute(ToolCall("set_volume", {"name": "up"}))  # pressing up twice is normal

    asyncio.run(go())
    assert sent == ["rafi", "rafi", "up", "up"] and "set_volume" not in ONCE_PER_RUN_TOOLS


def test_a_failed_one_shot_call_can_be_retried() -> None:
    attempts: list[int] = []

    def flaky(args, ctx):
        attempts.append(1)
        return ToolResult.failure("timeout", "no answer") if len(attempts) == 1 else ToolResult.success("sent")

    ex = ToolExecutor(ToolRegistry([Tool("send_email", "send", flaky, NameArgs, RiskLevel.SAFE)]),
                      PermissionEngine(PermissionsConfig()), ToolContext(Settings()))

    async def go():
        with runs.running(runs.Run("text", "x")):
            one = await ex.execute(ToolCall("send_email", {"name": "a"}))
            two = await ex.execute(ToolCall("send_email", {"name": "a"}))
        return one.result, two.result

    one, two = asyncio.run(go())
    assert not one.ok and two.ok and len(attempts) == 2  # only a SUCCESS blocks a repeat


# ------------------------------------------------------------------ the global automation pause

def test_automation_switch_pauses_a_mission_at_the_next_step(db) -> None:
    from app.core import emergency

    ex, _, log = make(db)
    store = MissionStore(db)
    mission = store.create("long job", validate_plan([step("look", "a"), step("look", "b")], ex.registry, ex.context))
    emergency.STATE.flags["automation_disabled"] = True
    try:
        outcome = asyncio.run(run(mission, store, ex, approved=True))
    finally:
        emergency.STATE.flags["automation_disabled"] = False
    assert not outcome.ok and "automations are switched off" in outcome.message and log == []
    assert store.get(mission.id).state == "paused"
    assert asyncio.run(run(store.get(mission.id), store, ex, approved=True)).ok and log == ["look:a", "look:b"]
