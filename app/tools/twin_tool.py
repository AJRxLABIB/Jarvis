"""The digital twin as a tool (rules in app/core/twin.py): refresh and show what Jarvis last observed (with staleness), forget it,
and preview what a plan or a mission would touch before it runs. Nothing here changes anything except the twin's own notes."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class TwinArgs(ToolArgs):
    action: Literal["show", "refresh", "what_if", "forget"] = Field(
        default="show",
        description="show = what Jarvis last observed, each with its source and age (old ones marked STALE); refresh = observe devices, "
                    "missions, disk and memory now; what_if = preview what a plan or mission would touch, its risk and undo options "
                    "WITHOUT running it; forget = clear the twin's observations")
    steps: str = Field(default="", max_length=6000, description='For what_if: the plan as JSON, e.g. [{"tool": "move_item", "arguments": {...}}]')
    mission_id: str = Field(default="", max_length=64, description="For what_if: preview the remaining steps of this mission instead")
    kind: str = Field(default="", max_length=20, description="For forget: only this kind (device, mission, disk, memory, path); empty = all")


async def twin_tool(args: TwinArgs, ctx: ToolContext) -> ToolResult:
    from app.core import twin

    if ctx.database is None:
        raise ToolError("The digital twin needs the local database.", "no_database")
    if args.action == "show":
        return ToolResult.success(twin.describe(ctx.database), observations=len(twin.snapshot(ctx.database)))
    if args.action == "refresh":
        notes = twin.refresh(ctx)
        return ToolResult.success("Observed: " + "; ".join(notes) + ".\n" + twin.describe(ctx.database), read=notes)
    if args.action == "forget":
        n = twin.forget(ctx.database, args.kind.strip() or None)
        return ToolResult.success(f"Cleared {n} observation(s). They are UNKNOWN again until observed.", cleared=n)
    steps: list[dict] = []
    if args.mission_id.strip():
        from app.core.missions import MissionStore

        mission = MissionStore(ctx.database).get(args.mission_id.strip())
        steps = [{"tool": s.tool, "arguments": s.arguments} for s in mission.steps if s.state in ("pending", "failed")]
    elif args.steps.strip():
        try:
            steps = json.loads(args.steps)
        except json.JSONDecodeError:
            raise ToolError('Steps must be JSON like [{"tool": "open_website", "arguments": {"site": "example.com"}}].', "invalid_steps") from None
        if not isinstance(steps, list) or not steps or not all(isinstance(s, dict) for s in steps):
            raise ToolError("Give a non-empty list of steps.", "invalid_steps")
    else:
        raise ToolError("Give steps or a mission_id to preview.", "missing_steps")
    report = twin.what_if(steps[:30], ctx)
    return ToolResult.success(report.describe(), blocked=report.blocked, highest_level=report.highest, simulated=True)


TOOLS: tuple[Tool, ...] = (
    Tool("digital_twin", "Show what Jarvis last observed about your environment (devices, missions, disk, memory) with source and age - "
         "stale readings are marked - or preview a plan/mission BEFORE it runs: what it would touch, risk, name collisions, undo options, "
         "likely failure modes and a safer alternative. An estimate, never a guarantee; nothing is run.",
         twin_tool, TwinArgs, RiskLevel.SAFE, "system", timeout=30),
)
