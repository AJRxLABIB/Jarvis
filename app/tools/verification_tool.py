"""``verification_coverage``: which tools can PROVE their result and which only report it. Reads the tool registry, runs nothing."""

from __future__ import annotations

from collections import defaultdict

from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolContext, ToolResult


def coverage(tools) -> dict:
    """{'verified': [...], 'unsupported': {category: [...]}, 'not_required': n} - SAFE read-only tools need no check; the rest should have one."""
    verified, unsupported, safe = [], defaultdict(list), 0
    for t in tools:
        if t.level == RiskLevel.SAFE and t.risk is None:
            safe += 1
        elif t.verify is not None:
            verified.append(t.name)
        else:
            unsupported[t.category].append(t.name)
    return {"verified": sorted(verified), "unsupported": {k: sorted(v) for k, v in sorted(unsupported.items())}, "not_required": safe}


def verification_coverage(_: NoArgs, ctx: ToolContext) -> ToolResult:
    registry = ctx.get_tool_registry()
    c = coverage(registry.tools)
    n_un = sum(len(v) for v in c["unsupported"].values())
    lines = [f"{len(c['verified'])} tools check the real world after they act; {n_un} tools that change things have no check of their own (their result is marked NOT_SUPPORTED, "
             f"never VERIFIED); {c['not_required']} read-only tools need none."]
    for cat, names in c["unsupported"].items():
        lines.append(f"  {cat}: {', '.join(names[:12])}" + (f" (+{len(names) - 12})" if len(names) > 12 else ""))
    return ToolResult.success("\n".join(lines), verified=len(c["verified"]), unsupported=n_un, not_required=c["not_required"])


TOOLS: tuple[Tool, ...] = (
    Tool("verification_coverage", "Show which tools verify their own results and which only report success ('which actions are verified', 'what can't you double-check').",
         verification_coverage, NoArgs, RiskLevel.SAFE, "memory", timeout=10),
)
