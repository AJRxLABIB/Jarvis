"""guardian: the cyber guardian's status, scan now, incidents, acknowledge / dismiss, accept-as-normal (app/core/guardian.py). It recommends; it never acts."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class GuardianArgs(ToolArgs):
    action: Literal["status", "scan", "incidents", "show", "acknowledge", "dismiss", "contained", "accept_baseline"] = Field(
        default="status", description="status = summary; scan = look now; incidents = list; show = one incident with evidence; acknowledge / dismiss / "
                                      "contained = mark one (you decide); accept_baseline = treat what is running now as normal")
    id: str = Field(default="", max_length=40, description="The incident id (or its start) for show / acknowledge / dismiss / contained")
    all: bool = Field(default=False, description="For incidents: include closed ones")


def _guardian(ctx: ToolContext):
    from app.core import guardian

    if ctx.database is None:
        raise ToolError("The guardian needs the local database.", "no_database")
    folder = ctx.paths.data_dir / "backups" if getattr(ctx, "paths", None) is not None else None
    return guardian, guardian.instance(ctx.settings, ctx.database, folder)


async def guardian_tool(args: GuardianArgs, ctx: ToolContext) -> ToolResult:
    guardian, g = _guardian(ctx)
    if args.action == "status":
        on = bool(getattr(ctx.settings, "cyber_guardian", False))
        return ToolResult.success(("" if on else "The guardian is OFF (turn on CYBER_GUARDIAN to scan every few minutes; a manual scan still works). ") + g.summary(),
                                  enabled=on)
    if args.action == "scan":
        found = await asyncio.to_thread(g.scan)
        if found:
            guardian.notify(ctx.database, found)
        stats = g.last_stats
        text = (f"Scan done. {len(found)} new incident(s):\n" + "\n".join(i.detail() for i in found)) if found else \
            "Scan done. Nothing new" + (" (the first scan only records what is normal for this PC)." if not stats else ".")
        return ToolResult.success(text, new=[i.id for i in found], stats=stats)
    store = g.store
    if args.action == "incidents":
        items = [i for i in store.incidents() if args.all or i.status == "open"]
        return ToolResult.success("\n".join(i.line() + f" - {i.title} [{i.id}]" for i in items[-15:]) or "No open incidents.", count=len(items))
    if args.action == "accept_baseline":
        return ToolResult.success(await asyncio.to_thread(g.accept_current))
    if not args.id:
        raise ToolError("Which incident? Give its id (see 'incidents').", "missing_id")
    if args.action == "show":
        hit = next((i for i in store.incidents() if i.id.startswith(args.id)), None)
        if hit is None:
            raise ToolError("No incident with that id.", "not_found")
        return ToolResult.success(hit.detail(), **hit.__dict__)
    status = {"acknowledge": "acknowledged", "dismiss": "dismissed", "contained": "contained"}[args.action]
    done = store.set_status(args.id, status)
    if done is None:
        raise ToolError("No incident with that id.", "not_found")
    return ToolResult.success(f"Marked {done.id} as {status}. (Nothing was stopped or changed on the PC.)", id=done.id, status=status)


TOOLS: tuple[Tool, ...] = (
    Tool("guardian", "The cyber guardian: is anything unusual happening on this PC (a burst of file changes, a new startup program, a new network listener, a "
         "stale or failed backup)? Scan now, list incidents with their evidence and confidence, mark them reviewed, or accept the current state as normal. "
         "It only watches and recommends - it never stops programs or deletes files, and it is not antivirus. For 'is my PC safe', 'any suspicious activity', "
         "'security check', 'what changed on my PC'.", guardian_tool, GuardianArgs, RiskLevel.SAFE, "system", timeout=60),
)
