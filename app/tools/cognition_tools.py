"""Round 92 tools: ``cognitive_plan`` (how much thinking a request needs, which roles, whether to ask first) and ``experiment_log`` (hypothesis -> change ->
observations -> a conclusion that cannot overstate). Cores: app/core/cognition.py, experiments.py (and interbrain.py for the message contract)."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from app.core import cognition as C, experiments as X
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class PlanArgs(ToolArgs):
    request: str = Field(min_length=1, max_length=2000, description="The request to assess, in the user's words")


async def cognitive_plan(args: PlanArgs, ctx: ToolContext) -> ToolResult:
    vram = None
    try:
        from app.core import perf

        snap = await asyncio.to_thread(perf.read_snapshot)
        vram = snap.vram_free_gb
    except Exception:
        pass
    p = C.classify(args.request, vram_free_gb=vram)
    return ToolResult.success(p.text(), level=p.level, objectives=len(p.objectives), brains=p.brains, clarify=bool(p.clarification))


class ExperimentArgs(ToolArgs):
    action: Literal["start", "observe", "conclude", "list", "delete"] = Field(default="list", description="start = record a hypothesis and the single change; observe = add what was seen; conclude = grade the evidence; list; delete")
    id: int = Field(default=0, ge=0, le=10**6, description="Experiment number (observe / conclude / delete)")
    hypothesis: str = Field(default="", max_length=300, description="start: what you think is the cause, e.g. 'the browser extension makes startup slow'")
    changed: str = Field(default="", max_length=200, description="start: the variable(s) you change, separated by ';' - change ONE where you can")
    baseline: str = Field(default="", max_length=300, description="start: the measurement BEFORE the change, e.g. 'startup 42 s'")
    expected: str = Field(default="", max_length=300, description="start: what you expect to see if the hypothesis is right")
    seen: str = Field(default="", max_length=200, description="observe: what was measured after the change")
    matches: bool = Field(default=True, description="observe: does it agree with the expectation?")
    note: str = Field(default="", max_length=300, description="conclude: your own remark to keep with the conclusion")


def _risk(args: ExperimentArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "delete" else RiskLevel.SAFE


def experiment_log(args: ExperimentArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    from app.memory.settings_store import SettingsStore

    st = X.Store(SettingsStore(ctx.database))
    if args.action == "list":
        items = st.all()
        return ToolResult.success("\n".join(X.describe(e) for e in items[-8:]) or "No experiments recorded yet.", count=len(items))
    if args.action == "start":
        if not args.hypothesis.strip():
            raise ToolError("What is the hypothesis?", "missing_hypothesis")
        e = st.start(args.hypothesis, [v for v in args.changed.split(";")], baseline=args.baseline, expected=args.expected)
        warn = " Several variables changed at once means a result can only show correlation." if len(e.variables_changed) > 1 else ""
        return ToolResult.success(f"Started experiment #{e.id}.{warn}", id=e.id)
    if not args.id:
        raise ToolError("Which experiment number?", "missing_id")
    if args.action == "delete":
        return ToolResult.success("Deleted." if st.remove(args.id) else "No such experiment.")
    e = st.observe(args.id, args.seen, args.matches) if args.action == "observe" else st.conclude(args.id, args.note)
    if e is None:
        raise ToolError("No such experiment.", "not_found")
    return ToolResult.success(X.describe(e), strength=e.strength)


TOOLS: tuple[Tool, ...] = (
    Tool("cognitive_plan", "Assess how much thinking a request needs (level 1-5 with a budget), split it into ordered objectives, name the specialist roles to use and say whether one "
         "short question must be asked first. Advisory only ('how hard is this request', 'break this task down').", cognitive_plan, PlanArgs, RiskLevel.SAFE, "memory", timeout=10),
    Tool("experiment_log", "Keep a scientific record when diagnosing a cause: hypothesis, the single change, baseline, observations, and a conclusion graded 'correlation only' / "
         "'supported' / 'contradicted' - never more certain than the evidence ('start an experiment', 'record what I saw').", experiment_log, ExperimentArgs, RiskLevel.SAFE, "memory",
         timeout=10, risk=_risk),
)
