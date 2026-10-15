"""'Undo your last action' and 'what can you undo' (app/core/undo.py has the rules)."""

from __future__ import annotations

from app.core import undo
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolContext, ToolError, ToolResult


def _store(ctx: ToolContext) -> undo.UndoStore:
    if ctx.database is None:
        raise ToolError("Undo needs the database.", "unavailable")
    return undo.UndoStore(ctx.database)


async def undo_last_action(_: NoArgs, ctx: ToolContext) -> ToolResult:
    store = _store(ctx)
    records = store.pending(1)
    if not records:
        return ToolResult.failure("nothing_to_undo", "There's nothing I can undo from the last 24 hours. (Opening "
                                  "apps, messages and commands can't be undone; deleted files are in the Recycle Bin.)")
    record = records[0]
    if ctx.executor is None:
        raise ToolError("Undo is not available here.", "unavailable")
    token = undo.UNDOING.set(True)
    try:
        execution = await ctx.executor.execute(store.call_of(record))
    finally:
        undo.UNDOING.reset(token)
    if not execution.result.ok:
        return ToolResult.failure(execution.result.error or "undo_failed",
                                  f"I couldn't {record['description']}: {execution.result.message}")
    store.mark_undone(record["id"])
    return ToolResult.success(f"Undone ({record['description']}).", undone=record["tool"])


def list_undoable(_: NoArgs, ctx: ToolContext) -> ToolResult:
    records = _store(ctx).pending(5)
    if not records:
        return ToolResult.success("Nothing to undo right now.", actions=[])
    steps = [r["description"] for r in records]
    return ToolResult.success("I can undo, newest first: " + "; ".join(steps) + ".", actions=steps)


TOOLS: tuple[Tool, ...] = (
    Tool("undo_last_action", "Undo Jarvis's last reversible action (move/rename/create a file, volume, brightness). "
         "Runs the exact inverse through the normal permission checks.", undo_last_action, NoArgs, RiskLevel.SAFE,
         category="basic", timeout=60),
    Tool("list_undoable", "What Jarvis can undo right now.", list_undoable, NoArgs, RiskLevel.SAFE, category="basic"),
)
