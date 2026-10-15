"""focus_mode tool: start, stop or ask about a focus session (the window runs it)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class FocusArgs(ToolArgs):
    action: Literal["start", "stop", "status"] = Field(default="start", description="start, stop, or how long is left")
    minutes: int = Field(default=25, ge=1, le=600, description="How long to focus (5-240 minutes)")
    close_distractions: bool = Field(default=False, description="true only if the user asks to close distracting "
                                                                "apps (games, chat)")


def _risk(args: FocusArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "start" and args.close_distractions else RiskLevel.SAFE


def _question(args: FocusArgs, ctx: ToolContext) -> str:
    return (f"Start a {args.minutes}-minute focus and close distracting apps ({ctx.settings.focus_apps})? "
            "Unsaved work in them may be lost.")


def focus_mode(args: FocusArgs, ctx: ToolContext) -> ToolResult:
    if ctx.focus is None:
        raise ToolError("Focus mode works in the Jarvis app.", "unavailable")
    return ToolResult.success(ctx.focus(args.action, args.minutes, args.close_distractions))


TOOLS = (
    Tool("focus_mode", "Focus mode: quiet time for N minutes (no suggestions or non-urgent alerts), optionally closing "
         "distracting apps; stop it or say how long is left. 'focus for 45 minutes', 'stop focus'.",
         focus_mode, FocusArgs, RiskLevel.SAFE, "todo", risk=_risk, confirmation=_question),
)
