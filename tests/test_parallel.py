"""Bounded parallel execution: limits, dependencies, failures, timeouts, cancellation, and the orchestrator rules."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core import orchestrator, parallel
from app.core.parallel import Job, run_jobs
from app.tools.base import ToolError
from tests.test_v3230 import make


def sleeper(log: list, name: str, seconds: float = 0.05, fail: bool = False):
    async def run():
        log.append(f"start:{name}")
        await asyncio.sleep(seconds)
        if fail:
            raise RuntimeError(f"{name} broke")
        log.append(f"end:{name}")
        return name

    return run


def test_jobs_run_concurrently_but_never_more_than_the_limit() -> None:
    log: list = []
    jobs = [Job(str(i), sleeper(log, str(i), 0.05)) for i in range(6)]
    started = time.monotonic()
    batch = asyncio.run(run_jobs(jobs, limit=3))
    elapsed = time.monotonic() - started
    assert batch.ok and batch.peak_parallel == 3
    assert elapsed < 0.25  # 6 x 0.05 s one after another would be 0.3 s
    assert [r.value for r in batch.results] == ["0", "1", "2", "3", "4", "5"]  # given order, not finishing order


def test_limit_one_is_sequential() -> None:
    log: list = []
    batch = asyncio.run(run_jobs([Job(str(i), sleeper(log, str(i), 0.01)) for i in range(3)], limit=1))
    assert batch.peak_parallel == 1 and log == ["start:0", "end:0", "start:1", "end:1", "start:2", "end:2"]


def test_dependencies_wait_and_a_failure_skips_dependents() -> None:
    log: list = []
    jobs = [Job("a", sleeper(log, "a", 0.05)), Job("b", sleeper(log, "b", 0.01), depends_on=("a",)),
            Job("c", sleeper(log, "c", 0.01, fail=True)), Job("d", sleeper(log, "d"), depends_on=("c",)),
            Job("e", sleeper(log, "e"), depends_on=("d",))]
    result = asyncio.run(run_jobs(jobs, limit=5)).by_id()
    assert log.index("end:a") < log.index("start:b")  # b waited for a
    assert result["b"].state == "done" and result["c"].state == "failed" and "c broke" in result["c"].error
    assert result["d"].state == "skipped" and "needs c" in result["d"].error
    assert result["e"].state == "skipped"  # skipping spreads: nothing runs on missing input
    assert "start:d" not in log and "start:e" not in log


def test_timeout_per_job_and_deadline_for_the_batch() -> None:
    log: list = []
    batch = asyncio.run(run_jobs([Job("slow", sleeper(log, "slow", 5), timeout=0.05), Job("ok", sleeper(log, "ok", 0.01))]))
    assert batch.by_id()["slow"].state == "timeout" and batch.by_id()["ok"].state == "done"
    late = asyncio.run(run_jobs([Job("1", sleeper(log, "1", 0.06)), Job("2", sleeper(log, "2", 0.01))], limit=1,
                                deadline_seconds=0.03))
    assert late.by_id()["2"].state == "not_run" and "time budget" in late.by_id()["2"].error


def test_cancelling_the_batch_stops_everything() -> None:
    log: list = []

    async def go():
        task = asyncio.ensure_future(run_jobs([Job(str(i), sleeper(log, str(i), 5)) for i in range(4)], limit=2))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert not any(entry.startswith("end:") for entry in log)  # nothing carried on in the background
    assert len([e for e in log if e.startswith("start:")]) == 2  # only the two that were allowed to start


def test_bad_job_graphs_are_refused() -> None:
    quick = sleeper([], "x", 0)
    with pytest.raises(ValueError, match="unique"):
        parallel.validate([Job("a", quick), Job("a", quick)])
    with pytest.raises(ValueError, match="unknown job"):
        parallel.validate([Job("a", quick, depends_on=("zzz",))])
    with pytest.raises(ValueError, match="loop"):
        parallel.validate([Job("a", quick, depends_on=("b",)), Job("b", quick, depends_on=("a",))])


# ------------------------------------------------------------------ orchestrator


def plan(ex, *steps):
    return orchestrator.validate_plan(list(steps), ex.registry)


def search(name, agent="researcher", tool="web_search"):
    return {"agent": agent, "tool": tool, "arguments": {"name": name}}


def test_read_only_steps_of_one_agent_can_run_together() -> None:
    ex, log = make()
    steps = plan(ex, search("a"), search("b"), search("c"))
    assert orchestrator.can_run_in_parallel(ex, steps)
    outcome = asyncio.run(orchestrator.run_plan(ex, "look things up", steps, parallel=3))
    assert outcome.ok and sorted(log) == ["web:a", "web:b", "web:c"] and "at a time" in outcome.message
    assert ex.context.agent_contract is None  # the contract was removed again


def test_state_changing_or_mixed_agent_plans_stay_sequential() -> None:
    ex, log = make()
    moves = plan(ex, {"agent": "organizer", "tool": "move_item", "arguments": {"name": "a"}},
                 {"agent": "organizer", "tool": "move_item", "arguments": {"name": "b"}})
    assert not orchestrator.can_run_in_parallel(ex, moves)  # a state-changing step never races another
    mixed = plan(ex, search("a"), search("b"))
    mixed[1].agent = "coder"  # two different agents: their contracts can't both be active at once
    assert not orchestrator.can_run_in_parallel(ex, mixed)
    assert not orchestrator.can_run_in_parallel(ex, plan(ex, search("only one")))
    outcome = asyncio.run(orchestrator.run_plan(ex, "g", moves, approved=True, parallel=4))
    assert outcome.ok and log == ["files:a", "files:b"]  # ran in order


def test_a_parallel_step_is_still_held_to_the_agents_contract() -> None:
    ex, log = make()
    steps = plan(ex, search("a"), search("b"))
    steps[1].tool = "move_item"  # tamper after validation: the executor still enforces the contract
    outcome = asyncio.run(orchestrator.run_plan(ex, "g", steps, parallel=2))
    assert steps[0].state == "done" and steps[1].state in ("failed", "not_run") and not outcome.ok
    assert "files:b" not in log


# ------------------------------------------------------------------ agent budgets and new agents


def test_an_agent_cannot_be_given_more_steps_than_its_limit() -> None:
    ex, _ = make()
    limit = orchestrator.contract_for("researcher").max_steps
    with pytest.raises(ToolError, match="at most"):
        plan(ex, *[search(str(i)) for i in range(limit + 1)])
    assert len(plan(ex, *[search(str(i)) for i in range(limit)])) == limit


def test_an_agent_that_used_its_time_budget_stops_the_plan(monkeypatch) -> None:
    from dataclasses import replace

    ex, log = make()
    tight = replace(orchestrator.contract_for("researcher"), max_seconds=0.0)
    from app.core import agents

    monkeypatch.setitem(agents.AGENTS, "researcher", tight)
    steps = plan(ex, search("a"), search("b"))
    outcome = asyncio.run(orchestrator.run_plan(ex, "g", steps))
    assert not outcome.ok and log == [] and all(s.state == "not_run" for s in steps)
    assert "budget" in outcome.message


def test_new_read_only_agents_exist_and_cannot_change_things() -> None:
    from app.core.agents import AGENTS, KNOWN_CATEGORIES
    from app.security.permissions import RiskLevel

    for name in ("reviewer", "verifier", "analyst"):
        contract = AGENTS[name]
        assert contract.max_risk == RiskLevel.SAFE and contract.categories <= KNOWN_CATEGORIES
        assert not contract.allows("git_commit", "dev", RiskLevel.MODERATE)[0]


def test_an_agent_cannot_hand_back_more_than_its_output_budget(monkeypatch) -> None:
    from dataclasses import replace

    from app.core import agents

    ex, log = make()
    monkeypatch.setitem(agents.AGENTS, "researcher", replace(agents.AGENTS["researcher"], max_tokens=2))
    steps = plan(ex, search("a"), search("b"))
    outcome = asyncio.run(orchestrator.run_plan(ex, "g", steps))
    assert steps[0].state == "done" and steps[1].state == "not_run" and "output budget" in outcome.message
    assert log == ["web:a"] and agents.estimate_tokens("x" * 8) == 2
