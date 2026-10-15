"""Your activity with Jarvis: "what was I doing before lunch?", "what did you do for me yesterday?"."""

from __future__ import annotations

from pydantic import Field

from app.agent import activity
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class RecallArgs(ToolArgs):
    period: str = Field(default="today", max_length=60,
                        description="When: today, this morning, before lunch, this afternoon, yesterday, "
                                    "yesterday evening, last night, last 2 hours, this week, or 'last session'")


def recall_activity(args: RecallArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("I have no activity log here.", "unavailable")
    now = ctx.clock()
    if "session" in args.period.lower() or "left off" in args.period.lower():
        actions, label = activity.last_session(ctx.database, now), "last session"
    else:
        found = activity.period(args.period, now)
        if found is None:
            raise ToolError(f"I can't tell which time '{args.period}' means. Try 'this morning', 'yesterday' or "
                            "'last 2 hours'.", "unknown_period")
        start, end, label = found
        actions = activity.load(ctx.database, start, end)
    return ToolResult.success(activity.summarize(actions, label), actions=len(actions))


TOOLS = (
    Tool("recall_activity", "What Jarvis did for the user in a time period (apps and sites opened, music, searches), "
         "from its own log. Use for 'what was I doing before lunch', 'what did I do yesterday'.",
         recall_activity, RecallArgs, RiskLevel.SAFE, "memory"),
)
