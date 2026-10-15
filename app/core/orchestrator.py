"""The agent orchestrator: split a job across specialist agents (app/core/agents.py) and run each part through
the SAME executor, scoped to that agent's contract.

A plan is a list of steps, each tagged with an agent and a tool call. Before anything runs, every step is
validated against its agent's contract (deterministically). At run time each step is executed with the executor's
full checks (permissions, taint firewall, confirmation, audit) AND the agent contract active, so the strictest of
the two always wins. The model may propose the plan; it can never widen what an agent is allowed to do.

This is deliberately NOT persistent like a mission - it's a here-and-now delegation. For long, restart-surviving
jobs, use a mission (app/core/missions.py); missions and agents share the same executor and safety kernel.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from app.ai.base import ToolCall
from app.core.agents import AgentContract, contract_for, estimate_tokens
from app.security.permissions import RiskLevel
from app.tools.base import ToolError

MAX_STEPS = 12
DEFAULT_BUDGET_SECONDS = 600.0  # the whole plan; each step also has its tool's own timeout


@dataclass
class AgentStep:
    agent: str
    tool: str
    arguments: dict[str, Any]
    title: str = ""
    state: str = "pending"
    result: str = ""


@dataclass
class TeamOutcome:
    goal: str
    steps: list[AgentStep]
    ok: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "ok": self.ok, "message": self.message,
                "steps": [{"agent": s.agent, "tool": s.tool, "title": s.title, "state": s.state,
                           "result": s.result[:300]} for s in self.steps]}


@contextmanager
def under_contract(context: Any, contract: AgentContract | None) -> Iterator[None]:
    """Run the enclosed executor calls with ``contract`` active on the context, then restore what was there."""
    previous = getattr(context, "agent_contract", None)
    context.agent_contract = contract
    try:
        yield
    finally:
        context.agent_contract = previous


def validate_plan(raw_steps: Any, registry: Any) -> list[AgentStep]:
    """Turn the model's (or user's) plan into checked AgentSteps, or raise ToolError. Each step's agent must
    exist and its tool must sit inside that agent's contract at the tool's base risk - a deterministic pre-check
    before anything runs (the executor re-checks the real, argument-based risk at run time)."""
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ToolError("Give at least one step (an agent and what it should do).", "invalid_plan")
    if len(raw_steps) > MAX_STEPS:
        raise ToolError(f"Too many steps (at most {MAX_STEPS}).", "invalid_plan")
    steps: list[AgentStep] = []
    seen: dict[tuple[str, str], int] = {}
    for i, item in enumerate(raw_steps, 1):
        if not isinstance(item, dict):
            raise ToolError(f"Step {i} isn't valid.", "invalid_plan")
        agent = str(item.get("agent", "")).strip().lower()
        contract = contract_for(agent)
        if contract is None:
            from app.core.agents import names

            raise ToolError(f"Step {i}: there's no '{agent}' agent. Agents: {', '.join(names())}.", "invalid_plan")
        tool_name = str(item.get("tool", "")).strip()
        tool = registry.get(tool_name)
        if tool is None:
            raise ToolError(f"Step {i}: there's no tool named '{tool_name}'.", "invalid_plan")
        ok, why = contract.allows(tool.name, tool.category, tool.level)
        if not ok:
            raise ToolError(f"Step {i}: {why}.", "invalid_plan")
        arguments = item.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ToolError(f"Step {i}: arguments must be an object.", "invalid_plan")
        title = " ".join(str(item.get("title") or f"{agent}: {tool_name}").split())[:120]
        step = AgentStep(agent, tool.name, arguments, title)
        key = (tool.name, json.dumps(arguments, sort_keys=True, default=str))
        if tool.level > RiskLevel.SAFE and key in seen:  # the same state-changing call twice is a duplicate side effect
            raise ToolError(f"Step {i} repeats step {seen[key]} ({tool_name} with the same arguments). "
                            "A state-changing step runs once per plan.", "invalid_plan")
        seen.setdefault(key, i)
        steps.append(step)
    for agent_name in {s.agent for s in steps}:
        contract = contract_for(agent_name)
        count = sum(1 for s in steps if s.agent == agent_name)
        if contract is not None and count > contract.max_steps:
            raise ToolError(f"The {agent_name} agent may have at most {contract.max_steps} steps in one plan "
                            f"(this plan gives it {count}). Split the job.", "invalid_plan")
    return steps


def can_run_in_parallel(executor: Any, steps: list[AgentStep]) -> bool:
    """Only independent, read-only steps of ONE agent may run together: the agent's contract is set on the shared
    context for the whole batch, and a state-changing step must never race another one."""
    if len(steps) < 2 or len({s.agent for s in steps}) != 1:
        return False
    for step in steps:
        tool = executor.registry.get(step.tool)
        if tool is None or tool.risk is not None or tool.level != RiskLevel.SAFE:
            return False
    return True


async def _run_parallel(executor: Any, goal: str, steps: list[AgentStep], limit: int, budget_seconds: float,
                        approved: bool) -> TeamOutcome:
    from app.core import parallel

    context = executor.context

    def job_for(step: AgentStep) -> parallel.Job:
        async def run() -> Any:
            execution = await executor.execute(ToolCall(step.tool, dict(step.arguments)), preapproved=approved)
            return execution.result

        return parallel.Job(str(id(step)), run)

    jobs = [job_for(s) for s in steps]
    with under_contract(context, contract_for(steps[0].agent)):
        batch = await parallel.run_jobs(jobs, limit=limit, deadline_seconds=budget_seconds)
    for step, res in zip(steps, batch.results):
        if res.state == parallel.DONE:
            step.result = res.value.message or (res.value.error or "")
            step.state = "done" if res.value.ok else "failed"
        else:
            step.state = "not_run" if res.state in (parallel.NOT_RUN, parallel.SKIPPED) else "failed"
            step.result = res.error
    done = sum(1 for s in steps if s.state == "done")
    ok = done == len(steps)
    return TeamOutcome(goal, steps, ok, f"{done}/{len(steps)} step(s) done (ran up to {batch.peak_parallel} at a time)" +
                       ("." if ok else " - some steps didn't finish."))


async def run_plan(executor: Any, goal: str, steps: list[AgentStep], *, approved: bool = False,
                   stop_on_error: bool = True, budget_seconds: float = DEFAULT_BUDGET_SECONDS,
                   parallel: int = 1) -> TeamOutcome:
    """Run each step through the executor under its agent's contract. Sequential; stops at the first failure
    unless stop_on_error is False. ``approved`` = the whole plan was approved up front, so MODERATE steps don't
    ask again (DANGEROUS still do; and the contract can only make things stricter). The plan has a total time
    budget; steps that never ran are marked 'not_run' (never reported as done), and cancellation is propagated."""
    if parallel > 1 and can_run_in_parallel(executor, steps):
        return await _run_parallel(executor, goal, steps, parallel, budget_seconds, approved)
    context = executor.context
    done = 0
    deadline = time.monotonic() + budget_seconds
    spent: dict[str, float] = {}  # seconds each agent has used in this plan
    tokens: dict[str, int] = {}   # estimated tokens of results each agent has handed back
    for step in steps:
        own = contract_for(step.agent)
        if own is not None and tokens.get(step.agent, 0) >= own.max_tokens:
            step.state, step.result = "not_run", f"the {step.agent} agent used up its output budget (~{own.max_tokens} tokens)"
            if stop_on_error:
                for rest in steps:
                    if rest.state == "pending":
                        rest.state, rest.result = "not_run", "an earlier step ran out of budget"
                return TeamOutcome(goal, steps, False, f"Stopped: {step.result}.")
            continue
        if own is not None and spent.get(step.agent, 0.0) >= own.max_seconds:
            step.state, step.result = "not_run", f"the {step.agent} agent used up its {int(own.max_seconds)} s budget"
            if stop_on_error:
                for rest in steps:
                    if rest.state == "pending":
                        rest.state, rest.result = "not_run", "an earlier step ran out of budget"
                return TeamOutcome(goal, steps, False, f"Stopped: {step.result}.")
            continue
        if time.monotonic() >= deadline:
            for rest in steps:
                if rest.state == "pending":
                    rest.state, rest.result = "not_run", "the plan ran out of time before this step"
            return TeamOutcome(goal, steps, False, f"Stopped: the {int(budget_seconds)} s time budget ran out "
                                                   f"after {done}/{len(steps)} step(s).")
        contract = contract_for(step.agent)
        step.state = "running"
        began = time.monotonic()
        try:
            with under_contract(context, contract):
                execution = await executor.execute(ToolCall(step.tool, dict(step.arguments)), preapproved=approved)
            spent[step.agent] = spent.get(step.agent, 0.0) + (time.monotonic() - began)
        except asyncio.CancelledError:
            for rest in steps:
                if rest.state in ("pending", "running"):
                    rest.state, rest.result = "not_run", "cancelled"
            raise
        result = execution.result
        step.result = result.message or (result.error or "")
        if own is not None:
            room = max(own.max_tokens - tokens.get(step.agent, 0), 0) * 4
            if len(step.result) > room:  # never hand back more than the agent's output budget
                step.result = step.result[:room] + " ...(cut: the agent's output budget)"
            tokens[step.agent] = tokens.get(step.agent, 0) + estimate_tokens(step.result)
        if result.ok:
            step.state = "done"
            done += 1
        else:
            step.state = "failed"
            if stop_on_error:
                return TeamOutcome(goal, steps, False,
                                   f"Stopped at '{step.title}' ({step.agent}): {step.result}")
    ok = all(s.state == "done" for s in steps)
    return TeamOutcome(goal, steps, ok,
                       f"{done}/{len(steps)} step(s) done" + ("." if ok else " - some steps didn't finish."))
