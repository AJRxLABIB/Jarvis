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


class WhatIfArgs(ToolArgs):
    hours_per_day: float = Field(default=6.0, ge=0.5, le=16, description="Productive hours available per day (default 6)")
    estimates: str = Field(default="", max_length=300, description="Effort per to-do id in hours, e.g. '3=2, 5=1.5'")
    postpone: str = Field(default="", max_length=200, description="Postpone to-dos: 'id=days', e.g. '4=3'")
    drop: str = Field(default="", max_length=100, description="To-do ids to drop in the scenario, e.g. '2,7'")
    add: str = Field(default="", max_length=300, description="New task in the scenario as 'text|hours|YYYY-MM-DD' (date optional); separate several with ';'")
    extra_daily_hours: float = Field(default=0.0, ge=0, le=12, description="Hours per day protected for something else, e.g. studying")
    extra_label: str = Field(default="", max_length=60, description="What the protected time is for (e.g. 'studying')")
    days: int = Field(default=14, ge=1, le=60, description="How many days ahead to simulate")


def _pairs(text: str) -> dict[str, float]:
    out = {}
    for part in text.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip()] = float(v)
            except ValueError:
                raise ToolError(f"'{part.strip()}' is not id=number.", "bad_argument")
    return out


def schedule_whatif(args: WhatIfArgs, ctx: ToolContext) -> ToolResult:
    from app.core import schedule_sim as S

    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    todos, _ = deadlines.load(ctx.database)
    today = ctx.clock().date() if callable(getattr(ctx, "clock", None)) else date.today()
    adds = []
    for part in [p for p in args.add.split(";") if p.strip()]:
        bits = [b.strip() for b in part.split("|")]
        try:
            adds.append({"text": bits[0], "hours": float(bits[1]) if len(bits) > 1 else 1.0, "due": bits[2] if len(bits) > 2 else None})
        except ValueError:
            raise ToolError(f"'{part.strip()}' should look like text|hours|YYYY-MM-DD.", "bad_argument")
    c = S.simulate(todos, today, args.hours_per_day, _pairs(args.estimates), {k: int(v) for k, v in _pairs(args.postpone).items()}, adds,
                   [x.strip() for x in args.drop.split(",") if x.strip()], args.extra_daily_hours, args.extra_label, horizon=args.days)
    return ToolResult.success(S.describe(c), confidence=c.confidence, changes=len(c.changes))


class AdapterArgs(ToolArgs):
    process: str = Field(min_length=1, max_length=60, description="Program or process name, e.g. 'excel', 'code', 'chrome', 'explorer'")
    action: str = Field(default="", max_length=60, description="Optional action to plan, e.g. 'save', 'new tab'. Leave empty to inspect the program.")


def app_adapter(args: AdapterArgs, ctx: ToolContext) -> ToolResult:
    from app.core.app_adapters import ApplicationAdapter

    ad = ApplicationAdapter.detect_application(args.process)
    if args.action:
        p = ad.plan_action(args.action)
        route = {"structured": f"use the '{p.tool}' tool", "control": "press it by its on-screen name", "shortcut": "offer the keyboard shortcut (not pressed silently)", "none": "no safe route"}[p.route]
        return ToolResult.success(f"{p.app} / {p.action}: {route}." + (f" Shortcut: {p.shortcut}." if p.shortcut else "") + (f" {p.note}" if p.note else ""), route=p.route)
    i = ad.inspect_application()
    acts = ", ".join(ad.get_available_actions()) or "none known"
    return ToolResult.success(f"{i.app}: UI Automation {i.uia}. Structured tools: {', '.join(i.structured) or 'none'}. Known actions: {acts}. {i.advice}".strip(), uia=i.uia)


TOOLS: tuple[Tool, ...] = (
    Tool("app_adapter", "How Jarvis can drive a desktop program: how well it exposes controls, which structured tool to prefer, known actions, and the safest route for one action "
         "('how can you control Excel', 'how would you save in VS Code'). Read-only; it plans, it does not press anything.", app_adapter, AdapterArgs, RiskLevel.SAFE, "windows", timeout=5),
    Tool("schedule_whatif", "Simulate the user's to-do schedule without changing it: 'can I finish everything this week', 'what if I postpone task 4 by 3 days', 'what if I study 2 hours a day'. "
         "Shows which tasks would miss their date, the assumptions and a confidence label.", schedule_whatif, WhatIfArgs, RiskLevel.SAFE, "memory", timeout=10),
    Tool("investigate_problem", "Investigate a problem with Jarvis or the PC from the evidence (logs, resources, checks, events): ranked causes marked CONFIRMED / STRONGLY_SUPPORTED / "
         "POSSIBLE / UNKNOWN, safe next steps, and a recheck after a fix ('why is it slow', 'investigate the microphone problem').", investigate, InvestigateArgs, RiskLevel.SAFE,
         "windows", timeout=40),
    Tool("deadline_risk", "List what is overdue, due soon, behind pace, blocked or overloaded across the user's todos and goals, with the rule behind each ('what am I late on', "
         "'deadline risks').", deadline_risk, NoArgs, RiskLevel.SAFE, "memory", timeout=10),
)
