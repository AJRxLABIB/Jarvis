"""Agents and the orchestrator (app/core/agents.py, app/core/orchestrator.py): delegate a job to a specialist
agent, or split it across several. Every step runs through the normal executor, scoped to the agent's contract -
an agent can never do more than its contract allows, whatever the plan says."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.core import orchestrator
from app.core.agents import AGENTS, DEFAULT_AGENT, contract_for
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _executor(ctx: ToolContext):
    if ctx.executor is None:
        raise ToolError("Agents need the assistant's executor.", "unavailable")
    return ctx.executor


def list_agents(_: NoArgs, ctx: ToolContext) -> ToolResult:
    return ToolResult.success("Your agents - " + "; ".join(a.summary() for a in AGENTS.values()),
                              agents=[{"name": a.name, "description": a.description,
                                       "categories": sorted(a.categories), "max_risk": a.max_risk.name.lower()}
                                      for a in AGENTS.values()])


class TeamStep(ToolArgs):
    agent: str = Field(description=f"Which agent: one of {', '.join(AGENTS)}")
    tool: str = Field(min_length=1, max_length=64, description="The tool this agent should call")
    arguments: dict[str, Any] = Field(default_factory=dict, description="The tool's arguments")
    title: str = Field(default="", max_length=120, description="What this step does, in a few words")


class DelegateStep(ToolArgs):
    tool: str = Field(min_length=1, max_length=64, description="The tool to call")
    arguments: dict[str, Any] = Field(default_factory=dict, description="The tool's arguments")
    title: str = Field(default="", max_length=120, description="What this step does, in a few words")


class TeamArgs(ToolArgs):
    goal: str = Field(min_length=2, max_length=200, description="What the team should achieve")
    steps: list[TeamStep] = Field(min_length=1, max_length=orchestrator.MAX_STEPS,
                                  description="The plan: each step names an agent and a tool call, in order")
    parallel: int = Field(default=1, ge=1, le=4, description="Run up to this many read-only steps of the same agent "
                                                             "at once (default 1 = one after another)")


class DelegateArgs(ToolArgs):
    agent: str = Field(default=DEFAULT_AGENT, description=f"Which agent: one of {', '.join(AGENTS)}")
    steps: list[DelegateStep] = Field(min_length=1, max_length=orchestrator.MAX_STEPS,
                                      description="The tool calls this agent should make, in order")
    parallel: int = Field(default=1, ge=1, le=4, description="Run up to this many read-only steps at once "
                                                             "(default 1 = one after another)")


def _plan(raw: list[dict], ctx: ToolContext):
    steps = orchestrator.validate_plan(raw, _executor(ctx).registry)
    highest = RiskLevel.SAFE
    for s in steps:
        tool = _executor(ctx).registry.get(s.tool)
        highest = RiskLevel(max(int(highest), int(tool.level)))
    return steps, highest


def _summary(steps) -> str:
    return "; ".join(f"{s.title}" for s in steps)


def _team_risk(args: TeamArgs, ctx: ToolContext) -> RiskLevel:
    _, highest = _plan([s.model_dump() for s in args.steps], ctx)
    return RiskLevel.DANGEROUS if highest >= RiskLevel.MODERATE else RiskLevel.SAFE


def _team_question(args: TeamArgs, ctx: ToolContext) -> str:
    try:
        steps, _ = _plan([s.model_dump() for s in args.steps], ctx)
    except ToolError as exc:
        return f"Run the team for '{args.goal}'? ({exc})"
    return f"Team plan for '{args.goal}': {_summary(steps)}. Approve?"


def _result(outcome) -> ToolResult:
    data = {k: v for k, v in outcome.to_dict().items() if k not in ("message", "ok")}
    if outcome.ok:
        return ToolResult.success(outcome.message, **data)
    return ToolResult.failure("team_incomplete", outcome.message, **data)


async def run_team(args: TeamArgs, ctx: ToolContext) -> ToolResult:
    steps, highest = _plan([s.model_dump() for s in args.steps], ctx)
    outcome = await orchestrator.run_plan(_executor(ctx), args.goal, steps,
                                          approved=highest >= RiskLevel.MODERATE, parallel=args.parallel)
    return _result(outcome)


def _delegate_plan(args: DelegateArgs, ctx: ToolContext):
    contract = contract_for(args.agent)
    if contract is None:
        raise ToolError(f"There's no '{args.agent}' agent. Agents: {', '.join(AGENTS)}.", "invalid_plan")
    raw = [{"agent": contract.name, "tool": s.tool, "arguments": s.arguments, "title": s.title} for s in args.steps]
    return _plan(raw, ctx)


def _delegate_risk(args: DelegateArgs, ctx: ToolContext) -> RiskLevel:
    _, highest = _delegate_plan(args, ctx)
    return RiskLevel.DANGEROUS if highest >= RiskLevel.MODERATE else RiskLevel.SAFE


def _delegate_question(args: DelegateArgs, ctx: ToolContext) -> str:
    try:
        steps, _ = _delegate_plan(args, ctx)
    except ToolError as exc:
        return f"Have the {args.agent} do this? ({exc})"
    return f"Have the {args.agent} agent: {_summary(steps)}. Approve?"


async def delegate(args: DelegateArgs, ctx: ToolContext) -> ToolResult:
    steps, highest = _delegate_plan(args, ctx)
    outcome = await orchestrator.run_plan(_executor(ctx), f"{args.agent} task", steps,
                                          approved=highest >= RiskLevel.MODERATE, parallel=args.parallel)
    return _result(outcome)


TOOLS: tuple[Tool, ...] = (
    Tool("list_agents", "The specialist agents you can delegate to (researcher, planner, organizer, coder, "
         "operator, secretary) and what each may do.", list_agents, NoArgs, RiskLevel.SAFE, "automation"),
    Tool("delegate", "Give one specialist agent a short list of tool calls to do now, scoped to what that agent "
         "is allowed to do. Jarvis validates the plan and asks before anything changes.", delegate, DelegateArgs,
         RiskLevel.SAFE, "automation", timeout=600, risk=_delegate_risk, confirmation=_delegate_question),
    Tool("run_team", "Split a job across several agents: each step names an agent and a tool call. Each step runs "
         "scoped to its agent's contract. Jarvis validates and asks before anything changes.", run_team, TeamArgs,
         RiskLevel.SAFE, "automation", timeout=900, risk=_team_risk, confirmation=_team_question),
)
