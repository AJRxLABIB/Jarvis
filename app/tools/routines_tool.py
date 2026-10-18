"""routine_suggestions tool: "what do I keep doing that you could automate?" (app/core/routines.py)."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from app.core import routines
from app.memory.settings_store import SettingsStore
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class RoutineArgs(ToolArgs):
    action: Literal["list", "accept", "dismiss"] = Field(
        default="list", description="list: what you keep doing at the same time; accept: turn one into a saved automation "
                                    "(asks first); dismiss: never suggest it again")
    key: str = Field(default="", max_length=20, description="The key shown in the list (for accept / dismiss)")


def _risk(args: RoutineArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "accept" else RiskLevel.SAFE


def _question(args: RoutineArgs, ctx: ToolContext) -> str:
    found = _find(ctx, args.key)
    return f"Create a daily automation for this? {found.describe()}"


def _found(ctx: ToolContext) -> list[routines.Routine]:
    if ctx.database is None:
        raise ToolError("I have no activity log here.", "unavailable")
    from app.automation.tasks import AutomationStore

    skip = routines.dismissed(SettingsStore(ctx.database)) | routines.automated_keys(AutomationStore(ctx.database))
    return routines.detect(routines.load(ctx.database, ctx.clock()), skip_keys=skip)


def _find(ctx: ToolContext, key: str) -> routines.Routine:
    for routine in _found(ctx):
        if routine.key == key.strip():
            return routine
    raise ToolError("I don't see that routine any more. Ask for the list again.", "unknown_routine")


def routine_suggestions(args: RoutineArgs, ctx: ToolContext) -> ToolResult:
    if args.action == "list":
        items = _found(ctx)
        if not items:
            return ToolResult.success("I haven't noticed anything you do at the same time on 3 or more days yet. I only "
                                      "see what Jarvis itself did for you, not apps you opened by hand.", routines=[])
        lines = [f"[{r.key}] {r.describe()}" for r in items]
        return ToolResult.success("Things you keep doing that I could automate:\n" + "\n".join(lines)
                                  + "\nSay 'automate <key>' or 'dismiss <key>'.",
                                  routines=[{"key": r.key, "tool": r.tool, "arguments": r.arguments, "time": r.time,
                                             "days": list(r.days), "occurrences": r.occurrences} for r in items])
    if not args.key.strip():
        raise ToolError("Which routine? Give its key from the list.", "missing_key")
    if args.action == "dismiss":
        if ctx.database is None:
            raise ToolError("I have no database here.", "unavailable")
        routines.dismiss(SettingsStore(ctx.database), args.key.strip())
        return ToolResult.success("Okay, I won't suggest that one again.")
    routine = _find(ctx, args.key)
    from app.tools.automation import ScheduleArgs, _schedule_precheck, schedule_task

    name = f"Routine: {routine.tool.replace('_', ' ')} {routine.time}"[:60]
    schedule = ScheduleArgs(name=name, steps=json.dumps([{"tool": routine.tool, "arguments": routine.arguments}]),
                            time=routine.time, days=",".join(("mon", "tue", "wed", "thu", "fri", "sat", "sun")[d] for d in routine.days))
    _schedule_precheck(schedule, ctx)                  # the same checks as creating any automation
    return schedule_task(schedule, ctx)


TOOLS = (
    Tool("routine_suggestions", "Routines the user repeats at the same time on several days (from Jarvis's own log), with an "
         "offer to make one a saved automation. 'what do I keep doing every morning', 'automate that', 'dismiss that'.",
         routine_suggestions, RoutineArgs, RiskLevel.SAFE, "automation", risk=_risk, confirmation=_question),
)
