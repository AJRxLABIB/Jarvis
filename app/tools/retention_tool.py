"""retention: what Jarvis keeps, for how long, and clearing one store (app/core/retention.py). Clearing asks first and says what it does not cover."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core import retention as R
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class RetentionArgs(ToolArgs):
    action: Literal["status", "clear"] = Field(default="status", description="status = what is kept and for how long; clear = remove ONE store's data")
    store: str = Field(default="", max_length=20, description=f"For clear: one of {', '.join(R.KEYS)}")


def retention(args: RetentionArgs, ctx: ToolContext) -> ToolResult:
    from app.memory.settings_store import SettingsStore

    if ctx.database is None:
        raise ToolError("This needs the local database.", "no_database")
    kv = SettingsStore(ctx.database)
    if args.action == "status":
        rows = R.status(ctx.database, ctx.settings, kv)
        return ToolResult.success(R.describe(rows), stores=[r.key for r in rows])
    if args.store not in R.KEYS:
        raise ToolError(f"Which store? One of: {', '.join(R.KEYS)}.", "bad_store")
    n, note = R.clear(args.store, ctx.database, ctx.settings, kv)
    return ToolResult.success(f"{n} item(s) removed. {note} {R.SCOPE_NOTE}", removed=n)


def _risk(args: RetentionArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "clear" else RiskLevel.SAFE


def _question(args: RetentionArgs, ctx: ToolContext) -> str:
    return f"Clear the stored '{args.store}' data? It cannot be undone. {R.SCOPE_NOTE}"


TOOLS: tuple[Tool, ...] = (
    Tool("retention", "Show what Jarvis keeps (notifications, event log, clipboard memory, health readings, recorded activity, finished tasks, closed incidents), "
         "how long, and clear one of them on request.", retention, RetentionArgs, RiskLevel.SAFE, "windows", timeout=30,
         risk=_risk, confirmation=_question),
)
