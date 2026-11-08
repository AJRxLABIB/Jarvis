"""``investigate_problem`` (symptom -> evidence -> ranked causes with honest confidence, then a recheck) and ``deadline_risk`` (overdue / due soon / behind pace / blocked / overloaded)."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Literal

from pydantic import Field

from app.core import deadlines, investigate as I
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult

_LAST: dict = {}


class InvestigateArgs(ToolArgs):
    action: Literal["investigate", "recheck"] = Field(default="investigate", description="investigate = look for causes of the symptom; recheck = after a fix, see whether the evidence is gone")
    symptom: str = Field(default="", max_length=300, description="What is wrong, in the user's words (e.g. 'answers are very slow', 'microphone not working')")


def _collectors(ctx: ToolContext):
    from app.core import event_intel, perf, resources, troubleshoot

    cols = [I.resource_collector(lambda: perf.read_snapshot(max_age=5), resources.read_gpu),
            I.finding_collector(lambda: troubleshoot.scan(ctx))]
    if ctx.paths is not None:
        cols.append(I.log_collector(ctx.paths.logs_dir))
    if ctx.database is not None:
        cols.append(I.event_collector(lambda: event_intel.recent(ctx.database, 60)))
    return cols


async def investigate(args: InvestigateArgs, ctx: ToolContext) -> ToolResult:
    cols = _collectors(ctx)
    if args.action == "recheck":
        prev = _LAST.get("inv")
        if prev is None:
            raise ToolError("There is no earlier investigation in this session to recheck.", "nothing_to_recheck")
        res = await asyncio.to_thread(I.recheck, prev, cols)
        return ToolResult.success("; ".join(f"{k}: {v.replace('_', ' ')}" for k, v in res.items()) or "Nothing to recheck.", result=res)
    if not args.symptom.strip():
        raise ToolError("Tell me what is wrong (e.g. 'answers are slow').", "missing_symptom")
    inv = await asyncio.to_thread(I.investigate, args.symptom, cols)
    _LAST["inv"] = inv
    if ctx.database is not None:
        from app.memory.settings_store import SettingsStore

        I.record(SettingsStore(ctx.database), inv)
    return ToolResult.success(inv.text(), top=[(h.id, h.confidence) for h in inv.hypotheses[:3]])


def deadline_risk(_: NoArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    todos, goals = deadlines.load(ctx.database)
    risks = deadlines.assess(todos, goals, ctx.clock().date() if callable(getattr(ctx, "clock", None)) else date.today())
    return ToolResult.success(deadlines.describe(risks), risks=len(risks))


TOOLS: tuple[Tool, ...] = (
    Tool("investigate_problem", "Investigate a problem with Jarvis or the PC from the evidence (logs, resources, checks, events): ranked causes marked CONFIRMED / STRONGLY_SUPPORTED / "
         "POSSIBLE / UNKNOWN, safe next steps, and a recheck after a fix ('why is it slow', 'investigate the microphone problem').", investigate, InvestigateArgs, RiskLevel.SAFE,
         "windows", timeout=40),
    Tool("deadline_risk", "List what is overdue, due soon, behind pace, blocked or overloaded across the user's todos and goals, with the rule behind each ('what am I late on', "
         "'deadline risks').", deadline_risk, NoArgs, RiskLevel.SAFE, "memory", timeout=10),
)
