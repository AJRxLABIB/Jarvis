"""Three read-only views over what the other parts already record:

* ``what_changed``  - what changed while you were away (app/core/away.py): events, notifications, missions, files, devices, security;
* ``situations``    - related events grouped into one explained situation each (app/core/event_intel.py);
* ``attention``     - whether Jarvis would interrupt you right now, and which kinds of background work the current mode allows (app/core/attention.py).

They never change anything except the "last active" marker that ``what_changed`` stores (so the next question starts from now).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _db(ctx: ToolContext):
    if ctx.database is None:
        raise ToolError("This needs the local database.", "no_database")
    return ctx.database


class ChangedArgs(ToolArgs):
    since: str = Field(default="", max_length=40, description="How far back: '3 hours', '2 days', 'yesterday'. Empty = since you were last active.")
    mark: bool = Field(default=True, description="Remember now as the new 'last active' time")


def _folders(ctx: ToolContext) -> list[str]:
    from app.core import guardian

    return [str(p) for p in guardian.watched_folders(getattr(ctx.settings, "guardian_folders", ""))]


async def what_changed(args: ChangedArgs, ctx: ToolContext) -> ToolResult:
    from app.core import away
    from app.memory.settings_store import SettingsStore

    db = _db(ctx)
    store = SettingsStore(db)
    now = datetime.now(timezone.utc)
    since = away.parse_since(args.since, now, away.marker(store))
    if since is None:
        return ToolResult.failure("no_start", "I don't know how far back to look - say for example 'since 3 hours ago' or 'since yesterday'." if args.since
                                  else "I have no 'last active' time yet. Say how far back to look, e.g. 'since yesterday'.")
    readers = away.real_readers(db, _folders(ctx), store)
    report = await asyncio.to_thread(away.compare, readers, since, now)
    if args.mark:
        away.mark_active(store, now, await asyncio.to_thread(readers.devices))
    return ToolResult.success(report.text(), empty=report.empty, unchecked=report.unchecked)


def _git_context(ctx: ToolContext, db):
    """Event diagnosis context: git changes (read-only) and earlier similar events. Any failure = that evidence is simply missing."""
    from app.core import event_intel, gitinfo

    try:
        from app.tools import developer as dev

        executor = dev._executor(ctx)

        def run(argv, cwd):
            r = executor.run(argv, cwd, timeout=20)
            return r.returncode, r.output

        def folder(project: str):
            try:
                return dev.project_folder(project, ctx)
            except Exception:
                return None

        return gitinfo.event_context(folder, run, lambda sig: gitinfo.count_similar(db, sig))
    except Exception:
        return event_intel.Context(seen_before=lambda sig: gitinfo.count_similar(db, sig))


class SituationArgs(ToolArgs):
    limit: int = Field(default=5, ge=1, le=15, description="How many situations to show")


async def situations(args: SituationArgs, ctx: ToolContext) -> ToolResult:
    from app.core import event_intel

    db = _db(ctx)
    found = await asyncio.to_thread(event_intel.situations, db, _git_context(ctx, db))
    if not found:
        return ToolResult.success("No open situations: nothing related or important happened recently.")
    return ToolResult.success("\n\n".join(d.text() for d in found[: args.limit]), count=len(found))


class AttentionArgs(ToolArgs):
    priority: Literal["low", "normal", "high", "urgent"] = Field(default="normal", description="Priority of the notice to test")
    category: str = Field(default="", max_length=20, description="Category of the notice, e.g. security, reminder, alert")


async def attention(args: AttentionArgs, ctx: ToolContext) -> ToolResult:
    from app.core import attention as A
    from app.core import modes, perf

    from app.core import meeting as M

    state = await asyncio.to_thread(A.fullscreen_reader())
    verdict = await asyncio.to_thread(M.in_meeting)
    when, why = A.interrupt(args.priority, args.category, fullscreen=bool(state), meeting=verdict.in_meeting)
    mode = modes.STATE.current
    plan = None
    try:
        plan = perf.current_plan(ctx.settings)
    except Exception:
        pass
    jobs = []
    for kind in ("background", "heavy", "network"):
        ok, reason = A.allow(kind, plan=plan)
        jobs.append(f"{kind}: {'allowed' if ok else 'waiting - ' + reason}")
    screen = {True: "a full-screen app is in front", False: "no full-screen app", None: "full-screen state unknown (not read on this PC)"}[state]
    return ToolResult.success(f"{mode.name.capitalize()} mode; {screen}; meeting: {'yes' if verdict.in_meeting else 'no'} ({verdict.reason}). A {args.priority} notice would be: {when}" + (f" ({why})" if why else "") + ".\n"
                              + "\n".join(jobs), when=when)


class ImpactArgs(ToolArgs):
    failed: list[str] = Field(default_factory=list, max_length=10, description="Services to treat as failed (e.g. ai_engine, database, disk); empty = use the real health checks")


async def service_impact(args: ImpactArgs, ctx: ToolContext) -> ToolResult:
    from app.core import diagnostics, service_health as SH

    failed = {f for f in args.failed if f in SH.GRAPH}
    if not args.failed:
        checks = await asyncio.to_thread(diagnostics.diagnose, ctx, include_network=True)
        failed = SH.failed_from_checks(checks)
    return ToolResult.success(SH.describe(failed), failed=sorted(failed))


TOOLS: tuple[Tool, ...] = (
    Tool("service_impact", "When something has failed (or you ask 'what if the AI engine stops'): which parts of Jarvis still work, which are limited and "
         "which are unavailable, with the recovery level that applies.", service_impact, ImpactArgs, RiskLevel.SAFE, "windows", timeout=30),
    Tool("what_changed", "Say what changed while you were away: events, notifications, missions, files in watched folders, devices and security "
         "incidents since a time you give (or since you were last active).", what_changed, ChangedArgs, RiskLevel.SAFE, "windows", timeout=60),
    Tool("situations", "Group recent related events (a failed build, its changes, the errors) into one explained situation with next steps.",
         situations, SituationArgs, RiskLevel.SAFE, "windows", timeout=30),
    Tool("attention", "Would Jarvis interrupt you right now, and which background work does the current mode allow?", attention, AttentionArgs,
         RiskLevel.SAFE, "windows", timeout=15),
)


class InteractionArgs(ToolArgs):
    pass


async def interaction_advice(args: InteractionArgs, ctx: ToolContext) -> ToolResult:
    from app.core import attention as A
    from app.core import interaction as I
    from app.core import meeting as M
    from app.core import modes

    s = ctx.settings
    verdict = await asyncio.to_thread(M.in_meeting)
    full = await asyncio.to_thread(A.fullscreen_reader())
    phone = None
    try:
        from app.core import device_mesh

        nodes = [n for n in device_mesh.snapshot(ctx.database) if n.kind == "phone"] if ctx.database is not None else []
        phone = any(n.connection in ("connected", "recent") for n in nodes) if nodes else False
    except Exception:
        phone = None
    advice = I.advise(I.Context(mode=modes.STATE.current.name, in_meeting=verdict.in_meeting, fullscreen=full,
                                gestures_enabled=bool(getattr(s, "gesture_control", False) or getattr(s, "gestures_enabled", False)), phone_connected=phone))
    return ToolResult.success(advice.text(), best=advice.ranked[0][0] if advice.ranked else "", speak=advice.speak_replies)


TOOLS = TOOLS + (
    Tool("interaction_advice", "Which way fits right now - voice, keyboard, gestures or your phone - and whether replies should be spoken (meeting, full-screen app, "
         "quiet / sleep mode).", interaction_advice, InteractionArgs, RiskLevel.SAFE, "windows", timeout=15),
)
