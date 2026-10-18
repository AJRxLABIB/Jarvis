"""ask_history tool: when / how many / what, answered from Jarvis's own records (app/core/history_qa.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.agent import activity
from app.core import history_qa
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class HistoryArgs(ToolArgs):
    kind: Literal["last", "count"] = Field(default="last", description="last: when did it last happen; count: how many times")
    what: str = Field(min_length=2, max_length=120, description="What to look for, in the user's words, e.g. 'run the backup', "
                                                                 "'open spotify', 'finish the report'")
    period: str = Field(default="", max_length=60, description="Optional: today, yesterday, this week, last 2 hours, this morning ...")


def ask_history(args: HistoryArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("I have no activity log here.", "unavailable")
    now = ctx.clock()
    since = until = None
    label = "so far"
    if args.period.strip():
        text = args.period.lower()
        if "week" in text and "last" not in text:
            since, until, label = history_qa.week_start(now), None, "this week"
        else:
            found = activity.period(args.period, now)
            if found is None:
                raise ToolError(f"I can't tell which time '{args.period}' means. Try 'today', 'yesterday', 'this week' or "
                                "'last 2 hours'.", "unknown_period")
            since, until, label = found
            label = f"({label})"
    if args.kind == "count":
        message, matches = history_qa.answer_count(ctx.database, args.what, now, since or now.replace(year=2000), until, label)
    else:
        message, matches = history_qa.answer_last(ctx.database, args.what, now, since, until)
    return ToolResult.success(message, records=[{"at": m.at.isoformat(timespec="minutes"), "what": m.what, "source": m.source}
                                                for m in matches])


TOOLS = (
    Tool("ask_history", "Answer 'when did I last ...' and 'how many times did I ...' from Jarvis's own record of what it did and "
         "the to-dos you finished (with the records it is based on). Not for apps the user opened by hand.",
         ask_history, HistoryArgs, RiskLevel.SAFE, "memory"),
)
