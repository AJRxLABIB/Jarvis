"""``root_cause`` (why a run failed: earliest failing step, ranked causes with evidence, recovery options with a level), ``inference_queue`` (the AI request queue as it really is)
and ``vault_scan`` (read an Obsidian-style notes folder: notes, tags, links, backlinks). All read-only."""

from __future__ import annotations

import asyncio

from pydantic import Field

from app.core import inference_gateway, replay, root_cause, vault
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult


class RootCauseArgs(ToolArgs):
    run_id: str = Field(default="", max_length=64, description="The run to diagnose; empty = the latest failed run")


async def root_cause_tool(args: RootCauseArgs, ctx: ToolContext) -> ToolResult:
    db = ctx.database
    if db is None:
        raise ToolError("Needs the local database.", "no_database")

    def work():
        if args.run_id.strip():
            return replay.build(db, args.run_id.strip())
        found = replay.failing_runs(db)
        return replay.build(db, found[0][0]["id"]) if found else None

    timeline = await asyncio.to_thread(work)
    if timeline is None:
        return ToolResult.success("No failed run to diagnose.", failures=0)
    d = root_cause.diagnose(timeline)
    return ToolResult.success(d.describe(), failures=0 if d.ok else 1, **({"cause": d.hypotheses[0].cls} if d.hypotheses else {}))


async def inference_queue(_: NoArgs, ctx: ToolContext) -> ToolResult:
    snap = inference_gateway.gateway().snapshot()
    lines = [f"Running/queued now: {len(snap['active'])}."] + [f"  {r[0]} {r[1]} {r[2] or '-'} {r[3]} ({r[4]}, {r[5]})" for r in snap["active"]]
    recent = snap["recent"]
    lines.append(f"Recent requests: {len(recent)}." if recent else "No AI requests have gone through the gateway yet (chat and voice still call the model directly).")
    lines += [f"  {r[0]} {r[1]} {r[2]} on {r[3] or '?'}" + (f", {r[4]} ms" if r[4] is not None else "") + (f" - {r[5]}" if r[5] else "") for r in recent]
    sch = snap["scheduler"]
    lines.append(f"Scheduler: {len(sch['queued'])} waiting, {len(sch['running'])} running, {sch['reserved_gb']} GB reserved" + (", background held" if sch["held_from"] is not None else "") + ".")
    return ToolResult.success("\n".join(lines), active=len(snap["active"]))


class VaultArgs(ToolArgs):
    path: str = Field(description="The notes folder (an Obsidian vault or any folder of .md files)", max_length=400)
    exclude: str = Field(default="", max_length=300, description="Comma-separated sub-folders to leave out")


async def vault_scan(args: VaultArgs, ctx: ToolContext) -> ToolResult:
    root = ctx.get_path_policy().resolve(args.path, must_exist=True)
    if not root.is_dir():
        raise ToolError("That is not a folder.", "invalid_arguments")
    v = await asyncio.to_thread(vault.scan, root, tuple(x.strip() for x in args.exclude.split(",") if x.strip()))
    return ToolResult.success(vault.summary(v), notes=len(v.notes), unresolved=len(v.unresolved))


TOOLS: tuple[Tool, ...] = (
    Tool("root_cause", "Diagnose why a run failed: the earliest failing step, the possible causes ranked by evidence (never certain), later failures marked as possible consequences, "
         "and recovery options with a level ('why did that fail', 'find the root cause').", root_cause_tool, RootCauseArgs, RiskLevel.SAFE, "memory", timeout=30),
    Tool("inference_queue", "Show the AI request queue: what is running or waiting, the device each used, recent latencies and scheduler holds ('what is the AI busy with').",
         inference_queue, NoArgs, RiskLevel.SAFE, "memory", timeout=10),
    Tool("vault_scan", "Read a folder of Markdown notes (Obsidian vault): note count, tags, links, backlinks and links to notes that do not exist.",
         vault_scan, VaultArgs, RiskLevel.SAFE, "files", timeout=60),
)
