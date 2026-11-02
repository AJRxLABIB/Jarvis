"""``wellness``: an optional, off-by-default local log of sleep, exercise, breaks, energy and mood with plain-statistics summaries.
Not medical advice. Nothing is collected unless you switch it on and say the numbers (app/core/wellness.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class WellArgs(ToolArgs):
    action: Literal["enable", "disable", "log", "summary", "insights", "delete_all"] = Field(
        description="enable / disable the log; log = record one number; summary = last 7 days; insights = patterns with their working; delete_all = erase every entry")
    metric: str = Field(default="", max_length=20, description="sleep_hours, exercise_min, breaks, energy (1-5), mood (1-5), water_glasses")
    value: float = Field(default=0, ge=0, le=1000, description="For log: the number (hours, minutes, count, or 1-5)")


def wellness(args: WellArgs, ctx: ToolContext) -> ToolResult:
    from app.core import wellness as W
    from app.memory.settings_store import SettingsStore

    if ctx.database is None:
        raise ToolError("The wellness log needs the local database.", "no_database")
    store = SettingsStore(ctx.database)
    a = args.action
    if a == "enable":
        W.set_enabled(store, True)
        return ToolResult.success("Wellness log is on. It only records numbers you tell me; no camera, microphone or sensors. Say 'turn off wellness' any time.")
    if a == "disable":
        W.set_enabled(store, False)
        return ToolResult.success("Wellness log is off. Your entries are kept until you say 'delete my wellness data'.")
    if a == "delete_all":
        W.delete_all(store)
        return ToolResult.success("All wellness entries were deleted.")
    if a == "log":
        try:
            day = W.log(store, args.metric.strip().lower(), args.value)
        except PermissionError as exc:
            raise ToolError(str(exc), "not_enabled") from exc
        except ValueError as exc:
            raise ToolError(str(exc), "bad_value") from exc
        return ToolResult.success(f"Logged {args.metric} = {args.value:g} for {day}.")
    if a == "summary":
        return ToolResult.success(W.summary(store))
    notes = W.insights(store)
    return ToolResult.success("\n".join(notes) + ("\n" + W.DISCLAIMER if W.enabled(store) else ""), notes=len(notes))


def _risk(args: WellArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("enable", "delete_all") else RiskLevel.SAFE


def _question(args: WellArgs, _ctx: ToolContext) -> str:
    return ("Delete ALL wellness entries?" if args.action == "delete_all" else
            "Turn on the wellness log? It stores only the numbers you tell Jarvis (sleep, exercise, breaks, energy, mood) on this PC.")


TOOLS: tuple[Tool, ...] = (
    Tool("wellness", "Optional wellness log (off until you enable it): record sleep, exercise, breaks, energy, mood; see averages and "
         "patterns with their working. Not medical advice.", wellness, WellArgs, RiskLevel.SAFE, "personal", timeout=15, risk=_risk, confirmation=_question),
)
