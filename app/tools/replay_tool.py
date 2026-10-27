"""The task debugger as a tool (rules in app/core/replay.py): list failing runs, show a run's timeline, find the first
divergence, simulate with mock tools, re-run one read-only call live, and manage which tools' details are hidden."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class ReplayArgs(ToolArgs):
    action: Literal["list", "show", "diagnose", "simulate", "live", "hide", "unhide"] = Field(
        default="diagnose",
        description="list = recent runs that went wrong; show = the recorded timeline (facts marked as recorded or "
                    "reconstructed); diagnose = the earliest divergence, likely cause and minimal recovery; simulate = "
                    "replay with MOCK tools and recorded results (nothing external runs); live = run ONE read-only call "
                    "again (asks you; never for anything that changes things); hide / unhide = whether a tool's "
                    "arguments and results are hidden in replays")
    run_id: str = Field(default="", max_length=64, description="The run (empty = the most recent run that went wrong)")
    call: int = Field(default=1, ge=1, le=200, description="For live: which call of the run (its number in 'show')")
    tool: str = Field(default="", max_length=60, description="For hide / unhide: the tool name")


def _db(ctx: ToolContext):
    if ctx.database is None:
        raise ToolError("The task debugger needs the local database.", "no_database")
    return ctx.database


def _pick(ctx: ToolContext, run_id: str):
    from app.core import replay

    db = _db(ctx)
    if run_id.strip():
        return replay.build(db, run_id.strip())
    found = replay.failing_runs(db)
    if not found:
        raise ToolError("No recent run went wrong, so there is nothing to debug. Give a run id to look at a specific one.", "not_found")
    return replay.build(db, found[0][0]["id"])


async def replay_tool(args: ReplayArgs, ctx: ToolContext) -> ToolResult:
    from app.core import replay

    db = _db(ctx)
    if args.action == "list":
        found = replay.failing_runs(db)
        if not found:
            return ToolResult.success("No recent run went wrong.", runs=[])
        lines = [f"{r['id']}  {str(r.get('started_at', ''))[:16]}  {r.get('state')}  \"{(r.get('text') or '')[:50]}\""
                 + (f"  -> call {d.call} {d.tool}: {d.kind}" if d else "") for r, d in found[:15]]
        return ToolResult.success("\n".join(lines), runs=[r["id"] for r, _ in found])
    if args.action in ("hide", "unhide"):
        if not args.tool.strip():
            raise ToolError("Say which tool.", "missing_name")
        now = replay.set_excluded(db, args.tool.strip(), args.action == "hide")
        return ToolResult.success(f"Replays {'hide' if args.action == 'hide' else 'show'} the arguments and results of {args.tool}. "
                                  f"Hidden tools: {', '.join(sorted(now))}.", hidden=sorted(now))
    t = _pick(ctx, args.run_id)
    if args.action == "show":
        return ToolResult.success("\n".join(replay.trace_lines(t)), run=t.run_id, calls=len(t.calls))
    if args.action == "diagnose":
        return ToolResult.success(replay.diagnose_text(t), run=t.run_id,
                                  divergence=None if t.divergence is None else {"call": t.divergence.call, "tool": t.divergence.tool,
                                                                                "kind": t.divergence.kind})
    if args.action == "simulate":
        sim = await replay.simulate(t)
        return ToolResult.success(sim.describe(), run=t.run_id, final_state=sim.final_state, matches=sim.matches, simulated=True)
    try:
        text = await replay.live(ctx, t, args.call)
    except replay.LiveRefused as exc:
        raise ToolError(str(exc), "not_repeatable") from None
    return ToolResult.success(text, run=t.run_id, call=args.call)


def _risk(args: ReplayArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("live", "unhide") else RiskLevel.SAFE


def _question(args: ReplayArgs, _ctx: ToolContext) -> str:
    if args.action == "unhide":
        return f"Stop hiding the arguments and results of {args.tool} in task replays?"
    return f"Re-run call {args.call} of that run now (read-only calls only; it is checked again for permission)?"


TOOLS: tuple[Tool, ...] = (
    Tool("task_replay", "Debug a task that went wrong: rebuild what happened from Jarvis's own record (facts marked as recorded vs "
         "reconstructed, missing data named), find the earliest divergence, suggest a minimal recovery, simulate with mock tools, "
         "or re-run one read-only call. Never replays anything that changes things, and hides sensitive tools' details.",
         replay_tool, ReplayArgs, RiskLevel.SAFE, "system", timeout=60, risk=_risk, confirmation=_question),
)
