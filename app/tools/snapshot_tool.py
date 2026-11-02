"""``work_snapshot``: remember a working session (folder, files, programs, mission, open questions), compare it with the real PC later, and
resume it safely - plus a recovery report of unfinished missions after a restart. Rules: app/core/snapshots.py."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class SnapArgs(ToolArgs):
    action: Literal["save", "list", "show", "resume", "recovery", "delete"] = Field(
        default="recovery", description="save = remember this session; list; show = compare a snapshot with the PC as it is now (changes marked); "
        "resume = open what still exists (asks first; closes and overwrites nothing; uncertain steps are never replayed); "
        "recovery = unfinished missions after a restart with the last confirmed step and what is uncertain; delete")
    name: str = Field(default="", max_length=40, description="The snapshot's name, e.g. 'thesis' (empty = the latest)")
    folder: str = Field(default="", max_length=400, description="For save: the project folder")
    files: list[str] = Field(default_factory=list, max_length=20, description="For save: the files you are working on")
    questions: list[str] = Field(default_factory=list, max_length=10, description="For save: open questions to remember")
    notes: str = Field(default="", max_length=500, description="For save: a note")


def _live(ctx: ToolContext):
    from app.core import snapshots as S

    def windows():
        try:
            from app.tools.layouts import snapshot as shot

            return shot()
        except Exception:
            return []

    return S.Live(windows=windows, mission=S.mission_view(ctx.database))


async def work_snapshot(args: SnapArgs, ctx: ToolContext) -> ToolResult:
    import asyncio
    from pathlib import Path

    from app.core import snapshots as S
    from app.memory.settings_store import SettingsStore

    if ctx.database is None:
        raise ToolError("Snapshots need the local database.", "no_database")
    store = SettingsStore(ctx.database)
    if args.action == "recovery":
        report = S.recovery_report(ctx.database)
        return ToolResult.success(S.recovery_text(report), unfinished=len(report), uncertain=sum(len(r["uncertain"]) for r in report))
    if args.action == "list":
        items = S._load(store)
        return ToolResult.success("\n".join(f"{s['name']} v{s['version']} ({s['taken_at'][:16].replace('T', ' ')} UTC)" for s in items)
                                  or "No snapshots yet.", snapshots=len(items))
    if args.action == "save":
        if not args.name.strip():
            raise ToolError("Give the snapshot a name.", "missing_name")
        policy = ctx.get_path_policy()
        folder = str(Path(policy.resolve(args.folder))) if args.folder.strip() else ""
        files = [str(Path(policy.resolve(f))) for f in args.files if f.strip()]
        from app.core.missions import MissionStore

        mission = None
        unfinished = MissionStore(ctx.database).unfinished()
        if unfinished is not None:
            view = S.mission_view(ctx.database)(unfinished.id) or {}
            mission = {"id": unfinished.id, "title": unfinished.title, "state": unfinished.state, "done": view.get("done"), "total": view.get("total")}
        windows = await asyncio.to_thread(_live(ctx).windows)
        try:
            snap = S.capture(name=args.name, folder=folder, files=files, windows=windows, mission=mission, questions=args.questions,
                             notes=args.notes, mtime=S.Live().mtime, previous_version=S.next_version(store, args.name))
        except ValueError as exc:
            raise ToolError(str(exc), "bad_name") from exc
        S.save(store, snap)
        return ToolResult.success(f"Saved '{snap['name']}' v{snap['version']}: {len(snap['files'])} file(s), {len(snap['windows'])} program(s)"
                                  + (f", mission '{mission['title']}'" if mission else "") + ". Window titles are not stored.", version=snap["version"])
    if args.action == "delete":
        if not args.name.strip():
            raise ToolError("Which snapshot?", "missing_name")
        return ToolResult.success(f"Deleted {S.delete(store, args.name)} version(s).")
    snap = S.latest(store, args.name)
    if snap is None:
        raise ToolError("There is no such snapshot." if args.name else "No snapshots yet. Save one first.", "not_found")
    findings = await asyncio.to_thread(S.compare, snap, _live(ctx))
    if args.action == "show":
        return ToolResult.success(S.describe(snap, findings), problems=sum(f.kind != "ok" and f.kind != "info" for f in findings))
    plan = S.resume_plan(snap, findings)
    if ctx.executor is None:
        raise ToolError("Resuming needs the tool executor.", "no_executor")
    from app.ai.base import ToolCall

    results = []
    for step in plan["steps"]:
        execution = await ctx.executor.execute(ToolCall(step["tool"], step["arguments"]))
        results.append((step["arguments"].get("path", ""), execution.result.ok))
    lines = [S.describe(snap, findings), f"Opened {sum(ok for _, ok in results)} of {len(results)}."]
    if plan["needs_your_decision"]:
        lines.append("Needs your decision (nothing was replayed): " + " | ".join(plan["needs_your_decision"]))
    if plan["skipped"]:
        lines.append("Left out: " + " | ".join(plan["skipped"]))
    return ToolResult.success("\n".join(lines), opened=sum(ok for _, ok in results), decisions=len(plan["needs_your_decision"]))


def _risk(args: SnapArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("resume", "delete") else RiskLevel.SAFE


def _question(args: SnapArgs, _ctx: ToolContext) -> str:
    if args.action == "delete":
        return f"Delete the snapshot '{args.name}'? (Only the saved note; no files are touched.)"
    return "Resume the saved work session: open its folder and files that still exist? Nothing is closed or overwritten."


TOOLS: tuple[Tool, ...] = (
    Tool("work_snapshot", "Remember a working session, compare it with the real PC later (what changed or is gone), resume it safely, or show "
         "unfinished missions after a restart with what is uncertain.", work_snapshot, SnapArgs, RiskLevel.SAFE, "system", timeout=60,
         risk=_risk, confirmation=_question),
)
