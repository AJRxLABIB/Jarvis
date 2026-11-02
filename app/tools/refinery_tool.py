"""``data_refinery``: scan folders you authorize for deadlines, tasks, decisions and labelled values, keep each with its source, and review
them before anything is trusted. Local rules, no model. Details: app/core/refinery.py."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class RefArgs(ToolArgs):
    action: Literal["scan", "review", "facts", "confirm", "reject", "remember", "conflicts"] = Field(
        description="scan = read new/changed files in a folder (incremental); review = what needs your eyes; facts = list (kind optional); "
                    "confirm / reject = your decision on one fact (id); remember = copy a CONFIRMED fact into memory; conflicts = differing values")
    folder: str = Field(default="", max_length=400, description="For scan: the folder (inside the folders Jarvis may read)")
    fact_id: str = Field(default="", max_length=20, description="The fact's id (first 6+ characters)")
    kind: str = Field(default="", max_length=10, description="For facts: deadline, task, decision or value")


def data_refinery(args: RefArgs, ctx: ToolContext) -> ToolResult:
    from app.core import refinery as R
    from app.memory.settings_store import SettingsStore

    if ctx.database is None:
        raise ToolError("The refinery needs the local database.", "no_database")
    store = SettingsStore(ctx.database)
    a = args.action
    if a == "scan":
        if not args.folder.strip():
            raise ToolError("Which folder?", "missing_folder")
        folder = Path(ctx.get_path_policy().resolve(args.folder))
        from app.memory import sources
        from app.memory.semantic import is_sensitive, read_document

        patterns = sources.exclusions(ctx.database)
        report = R.run(store, [folder], reader=read_document, sensitive=is_sensitive, excluded=lambda p: sources.is_excluded(p, patterns))
        queue = R.review_queue(R.all_facts(store))
        return ToolResult.success(report.describe() + (f" {len(queue)} item(s) need your review." if queue else ""), review=len(queue),
                                  added=report.facts_added, stale=report.facts_stale)
    facts = R.all_facts(store)
    if a == "review":
        queue = R.review_queue(facts)
        return ToolResult.success("\n".join(f"{R.describe_fact(f)} - {why}" for f, why in queue[:15]) or "Nothing needs review.", review=len(queue))
    if a == "conflicts":
        pairs = R.conflicts(facts)
        return ToolResult.success("\n".join(f"{a.key}: '{a.value}' ({Path(a.source).name}) vs '{b.value}' ({Path(b.source).name})" for a, b in pairs[:10])
                                  or "No conflicting values.", conflicts=len(pairs))
    if a == "facts":
        live = [f for f in facts if f.status != "rejected" and (not args.kind or f.kind == args.kind.lower())]
        return ToolResult.success("\n".join(R.describe_fact(f) for f in live[-20:]) or "No facts yet. Scan a folder first.", count=len(live))
    if not args.fact_id.strip():
        raise ToolError("Which fact (its id from 'review' or 'facts')?", "missing_id")
    try:
        if a in ("confirm", "reject"):
            f = R.set_status(store, args.fact_id.strip(), "confirmed" if a == "confirm" else "rejected")
            return ToolResult.success(f"{'Confirmed' if a == 'confirm' else 'Rejected'}: {f.text}")
        from app.memory.manager import MemoryManager

        return ToolResult.success("Remembered: " + R.promote(store, MemoryManager(ctx.database), args.fact_id.strip()))
    except KeyError as exc:
        raise ToolError("There is no such fact id.", "not_found") from exc
    except ValueError as exc:
        raise ToolError(str(exc), "not_confirmed") from exc


def _risk(args: RefArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "remember" else RiskLevel.SAFE


def _question(args: RefArgs, _ctx: ToolContext) -> str:
    return "Copy this confirmed fact into Jarvis's memory (with its source)?"


TOOLS: tuple[Tool, ...] = (
    Tool("data_refinery", "Scan a folder for deadlines, tasks, decisions and labelled values (rules, local), keep each with its source file and line, "
         "flag stale facts and conflicting values, and review before anything is trusted or remembered.", data_refinery, RefArgs, RiskLevel.SAFE,
         "files", timeout=120, risk=_risk, confirmation=_question),
)
