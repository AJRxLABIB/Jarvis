"""Control what Jarvis may index and search (rules in app/memory/sources.py): exclusion patterns, per-source switches, near-duplicate
report, and removal of already-indexed material that you excluded."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class SourcesArgs(ToolArgs):
    action: Literal["show", "exclude", "include", "disable", "enable", "duplicates", "purge"] = Field(
        default="show",
        description="show = current exclusions and source switches; exclude / include = add or remove a folder or name pattern "
                    "(e.g. 'D:/Private/*', '*.bak') that is never indexed or returned; disable / enable = leave memories, documents "
                    "or conversations out of (or back in) the combined search; duplicates = documents that are near-copies of each "
                    "other (reporting only); purge = delete already-indexed passages from excluded paths (asks first)")
    value: str = Field(default="", max_length=200, description="For exclude / include: the pattern. For disable / enable: memory, document or conversation")


def sources_tool(args: SourcesArgs, ctx: ToolContext) -> ToolResult:
    from app.memory import sources as S

    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    db = ctx.database
    if args.action == "exclude":
        now = S.add_exclusion(db, args.value)
        return ToolResult.success(f"Never indexed or returned from now on: {', '.join(now)}. Say 'purge' to also delete what was already indexed.", exclusions=now)
    if args.action == "include":
        now = S.remove_exclusion(db, args.value)
        return ToolResult.success(f"Excluded: {', '.join(now) or 'nothing'}. Re-index the folder to bring its documents back.", exclusions=now)
    if args.action in ("disable", "enable"):
        off = S.set_kind(db, args.value.strip().lower(), args.action == "enable")
        return ToolResult.success(f"Left out of the combined search: {', '.join(sorted(off)) or 'nothing'}.", disabled=sorted(off))
    if args.action == "purge":
        n = S.purge_excluded(db)
        return ToolResult.success(f"Removed {n} excluded document(s) from the index.", removed=n)
    if args.action == "duplicates":
        pairs = S.near_duplicates(db)
        if not pairs:
            return ToolResult.success("No near-duplicate documents found (similarity is by wording, so paraphrases are not caught).", pairs=[])
        return ToolResult.success("Near-copies (nothing was deleted):\n" + "\n".join(f"{int(s * 100)}%  {a}  <->  {b}" for a, b, s in pairs),
                                  pairs=[list(p) for p in pairs])
    ex, off = S.exclusions(db), sorted(S.disabled_kinds(db))
    return ToolResult.success(f"Excluded: {', '.join(ex) or 'none'}. Left out of the combined search: {', '.join(off) or 'nothing'}.",
                              exclusions=ex, disabled=off)


def _risk(args: SourcesArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "purge" else RiskLevel.SAFE


def _question(args: SourcesArgs, _ctx: ToolContext) -> str:
    return "Delete the already-indexed passages of every excluded path from Jarvis's document index? (The files themselves are not touched.)"


TOOLS: tuple[Tool, ...] = (
    Tool("retrieval_sources", "Control what Jarvis may index and search: exclude folders or patterns, leave memories / documents / "
         "conversations out of the combined search, list near-duplicate documents, purge excluded material from the index.",
         sources_tool, SourcesArgs, RiskLevel.SAFE, "memory", timeout=30, risk=_risk, confirmation=_question),
)
