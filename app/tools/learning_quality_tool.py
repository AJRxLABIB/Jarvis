"""learning_audit: check what Jarvis has learned for harmful, contradicting or faded items; clean up (reversible); undo (app/core/learning_quality.py)."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from app.core import learning_quality as Q
from app.memory.settings_store import SettingsStore
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class AuditArgs(ToolArgs):
    action: Literal["audit", "cleanup", "undo", "hard_cases", "history"] = Field(
        default="audit", description="audit = look for harmful / contradicting / faded learned items; cleanup = retire them (reversible); "
                                     "undo = bring back the last cleanup; hard_cases = corrected mistakes used as tests; history = past cleanups")


def _rows(ctx: ToolContext) -> tuple[list[dict], list[dict]]:
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    with ctx.database.connect() as conn:
        items = [dict(r) for r in conn.execute("SELECT * FROM growth_items")]
        turns = [dict(r) for r in conn.execute("SELECT * FROM learning_turns ORDER BY id DESC LIMIT 1500")]
    return items, turns


def learning_audit(args: AuditArgs, ctx: ToolContext) -> ToolResult:
    items, turns = _rows(ctx)
    store = SettingsStore(ctx.database)
    if args.action == "undo":
        n = Q.restore_last(ctx.database, store)
        return ToolResult.success(f"Brought back {n} item(s)." if n else "No cleanup to undo.", restored=n)
    if args.action == "history":
        return ToolResult.success(json.dumps(store.get(Q.LEDGER_KEY, []) or [])[:1500] or "[]")
    report = Q.audit(items, turns)
    if args.action == "hard_cases":
        return ToolResult.success("\n".join(f"{c['request']} -> {c['expect_tool']} (not {c['wrong_tool'] or 'a plain answer'})"
                                            for c in report["hard_cases"]) or "No corrected mistakes yet.", cases=report["hard_cases"])
    if args.action == "cleanup":
        n = Q.retire(ctx.database, store, report["retire"], "harmful, contradicting or faded")
        return ToolResult.success(f"Retired {n} item(s); 'undo' brings them back. Your request history is untouched.", retired=n)
    return ToolResult.success(Q.describe(report), **{k: len(v) if isinstance(v, list) else v for k, v in report.items()})


TOOLS: tuple[Tool, ...] = (
    Tool("learning_audit", "Check what Jarvis learned from use for items that made things worse, contradict a better one or faded away; clean them "
         "up (reversible) ('audit your learning', 'is anything you learned wrong', 'clean up what you learned').",
         learning_audit, AuditArgs, RiskLevel.MODERATE, "memory",
         confirmation=lambda a, _: "Retire learned items that hurt, contradict or faded? (You can undo it.)"),
)
