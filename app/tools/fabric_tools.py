"""Round 91 tools: ``ui_drift`` (did a program's interface change, and what replaced the control), ``place_task`` (which trusted device should do this
work) and ``doc_to_plan`` (turn a document's recommendations into evidence-checked proposals). Cores: app/core/ui_drift.py, task_placement.py, knowledge_action.py."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.core import knowledge_action as KA, task_placement as TP, ui_drift as UD
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class DriftArgs(ToolArgs):
    action: Literal["check", "learn", "resolve", "versions", "rollback"] = Field(
        default="check", description="check = compare the foreground program with its saved profile; learn = save its current controls as a new profile version; "
        "resolve = find the replacement for a control that moved; versions = saved versions; rollback = drop the newest saved version")
    app: str = Field(default="", max_length=60, description="Program name for versions / rollback (default: the foreground program)")
    control: str = Field(default="", max_length=120, description="resolve: the control's old name, e.g. 'Save'")
    role: str = Field(default="", max_length=40, description="resolve: the control's role, e.g. 'button' (optional)")
    auto_id: str = Field(default="", max_length=120, description="resolve: the control's automation id (optional)")
    version: str = Field(default="", max_length=40, description="learn: the program's version, if known")


def _drift_risk(args: DriftArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("learn", "rollback") else RiskLevel.SAFE


async def ui_drift(args: DriftArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    from app.memory.settings_store import SettingsStore

    store = UD.ProfileStore(SettingsStore(ctx.database))
    if args.action in ("versions", "rollback") and args.app.strip():
        app = args.app
    else:
        try:
            live = await asyncio.wait_for(asyncio.to_thread(UD.read_live), 20)
        except Exception as exc:
            if args.action in ("versions", "rollback") and not args.app.strip():
                raise ToolError("Say which program (app) - the foreground one could not be read.", "missing_app")
            raise ToolError(f"Could not read the foreground window's controls: {exc}", "ui_unavailable")
        app = args.app.strip() or live.app
    if args.action == "versions":
        v = store.versions(app)
        return ToolResult.success(f"{app}: {len(v)} saved version(s)" + (f"; newest has {len(v[-1].controls)} controls." if v else ". Use learn to save one."), versions=len(v))
    if args.action == "rollback":
        ok = store.rollback(app)
        return ToolResult.success(f"Rolled back {app} to its previous profile." if ok else f"{app} has no earlier version to go back to.", rolled_back=ok)
    if args.action == "learn":
        live.version = args.version.strip()
        n = store.learn(live)
        return ToolResult.success(f"Saved {live.app} profile version {n} ({len(live.controls)} controls).", version=n)
    saved = store.current(live.app)
    if saved is None:
        return ToolResult.success(f"No saved profile for {live.app} yet. Use learn first.", known=False)
    if args.action == "check":
        d = UD.diff(saved, live)
        return ToolResult.success(UD.describe(live.app, d), compatibility=d.compatibility, changed=len(d.changed), removed=len(d.removed))
    if not args.control.strip():
        raise ToolError("Which control? Give its old name.", "missing_control")
    r = UD.resolve(UD.Control(args.control.strip(), args.role.strip(), args.auto_id.strip()), live.controls)
    lines = [f"{c.control.name!r} ({c.control.role or 'control'}) {c.confidence:.0%}: {', '.join(c.why)}" for c in r.candidates[:4]]
    return ToolResult.success((f"{'SAFE' if r.safe else 'UNCERTAIN'} - {r.note}.\n" + "\n".join(lines)).strip(), safe=r.safe, candidates=len(r.candidates))


class PlaceArgs(ToolArgs):
    needs: str = Field(default="", max_length=200, description="Abilities the task needs, comma separated (e.g. 'camera, notifications' or 'desktop automation')")
    privacy: Literal["public", "personal", "private"] = Field(default="personal", description="private = only this PC may do it; personal = any trusted device of the user; public = any")
    ram_mb: int = Field(default=0, ge=0, le=262144, description="Memory the task needs, in MB (0 = small)")
    vram_mb: int = Field(default=0, ge=0, le=65536, description="GPU memory the task needs, in MB (0 = no GPU)")
    minutes: int = Field(default=1, ge=1, le=1440, description="How long the task will run, in minutes")


async def place_task(args: PlaceArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    from app.core import device_mesh as M, perf

    nodes = await asyncio.to_thread(M.snapshot, ctx.database, None)
    ram = vram = None
    try:
        snap = await asyncio.to_thread(perf.read_snapshot)
        ram = int(snap.ram_free_gb * 1024) if snap.ram_free_gb is not None else None
        vram = int(snap.vram_free_gb * 1024) if snap.vram_free_gb is not None else None
    except Exception:
        pass
    devices = TP.from_mesh(nodes, ram, vram)
    needs = frozenset(x.strip() for x in args.needs.split(",") if x.strip())
    p = TP.place(TP.Task(needs, args.privacy, args.ram_mb, args.vram_mb, args.minutes), devices)
    return ToolResult.success(p.text(), device=p.chosen.device.name if p.chosen else None, considered=len(devices))


class DocPlanArgs(ToolArgs):
    text: str = Field(min_length=20, max_length=60000, description="The document or research text to analyse (paste it, or read the file first)")


def doc_to_plan(args: DocPlanArgs, ctx: ToolContext) -> ToolResult:
    root = Path(__file__).resolve().parents[2]
    text = KA.report(args.text, root if (root / "app").is_dir() else None)
    if getattr(ctx, "database", None) is not None:     # earlier accept / reject decisions about technologies this document mentions
        try:
            from app.memory.settings_store import SettingsStore

            known = KA.TechDecisions(SettingsStore(ctx.database)).find(args.text)
            if known:
                text += "\n\nEarlier decisions about things mentioned here:\n" + "\n".join("- " + KA.describe_decision(d) for d in known[:6])
        except Exception:
            pass
    return ToolResult.success(text, source="untrusted_document")


class DecisionArgs(ToolArgs):
    action: Literal["record", "list", "due", "delete"] = Field(default="list", description="record = remember why a technology was accepted / rejected / deferred; list; due = decisions "
                                                               "that should be re-checked now; delete")
    technology: str = Field(default="", max_length=80, description="record / delete: the technology or library, e.g. 'Windows Sandbox', 'Graphiti'")
    verdict: Literal["accepted", "rejected", "deferred"] = Field(default="deferred", description="record: the decision")
    reason: str = Field(default="", max_length=300, description="record: why, in one sentence")
    revisit_days: int = Field(default=0, ge=0, le=3650, description="record: look again after this many days (0 = never)")


def _decision_risk(args: DecisionArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "delete" else RiskLevel.SAFE


def tech_decision(args: DecisionArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    from app.memory.settings_store import SettingsStore

    st = KA.TechDecisions(SettingsStore(ctx.database))
    if args.action == "record":
        try:
            d = st.record(args.technology, args.verdict, args.reason, args.revisit_days)
        except ValueError as exc:
            raise ToolError(str(exc), "bad_argument")
        return ToolResult.success("Recorded. " + KA.describe_decision(d))
    if args.action == "delete":
        return ToolResult.success("Deleted." if st.remove(args.technology) else "No decision recorded for that.")
    items = st.due() if args.action == "due" else st.all()
    empty = "Nothing is due for a re-check." if args.action == "due" else "No technology decisions recorded yet."
    return ToolResult.success("\n".join(KA.describe_decision(d) for d in items[-20:]) or empty, count=len(items))


class VerifyArgs(ToolArgs):
    kind: Literal["file", "git"] = Field(description="file = check a file Jarvis says it saved; git = check a repository Jarvis says it changed")
    path: str = Field(min_length=1, max_length=400, description="The file, or the project folder for git")
    contains: str = Field(default="", max_length=500, description="file: text that must be inside, separated by ';'")
    min_bytes: int = Field(default=1, ge=0, le=10**9, description="file: smallest acceptable size in bytes")
    within_minutes: int = Field(default=0, ge=0, le=100000, description="file: it must have been written within this many minutes (0 = any time)")
    branch: str = Field(default="", max_length=100, description="git: the branch it should be on")
    message: str = Field(default="", max_length=200, description="git: text the latest commit message must contain")
    files: str = Field(default="", max_length=800, description="git: files the last commit should touch, separated by ';' (empty = do not check)")
    clean: Literal["", "yes", "no"] = Field(default="", description="git: yes = the working tree must be clean, no = it must have changes")


def verify_claim(args: VerifyArgs, ctx: ToolContext) -> ToolResult:
    from app.core import claim_verify as CV

    path = ctx.get_path_policy().resolve(args.path, must_exist=True)
    if args.kind == "file":
        v = CV.check_file(path, [c.strip() for c in args.contains.split(";") if c.strip()], args.min_bytes, args.within_minutes * 60 or None)
    else:
        from app.tools import developer as dev

        executor = dev._executor(ctx)

        def run(argv, cwd):
            r = executor.run(argv, cwd, timeout=20)
            return r.returncode, r.output

        v = CV.check_git(path, run, args.branch.strip(), args.message.strip(), [f for f in args.files.split(";") if f.strip()] if args.files.strip() else None,
                         {"yes": True, "no": False}.get(args.clean))
    if ctx.database is not None:                       # every independent check leaves an evidence record (core/action_evidence.py)
        try:
            from app.core import action_evidence as AE

            failed = "; ".join(f"{what}: {detail}" for what, ok, detail in v.checks if not ok)[:300]
            AE.Store(ctx.database).add(AE.Evidence(action=f"verify_{args.kind}", verifier=f"claim_verify.check_{args.kind}", source="filesystem" if args.kind == "file" else "git",
                                                   status={"verified": "pass", "failed": "fail"}.get(v.status, "unreadable"), target=path.name,
                                                   expected=(args.contains or args.message or args.branch)[:200], observed=failed or "all checks passed"))
        except Exception:
            pass                                       # recording must never break the check itself
    return ToolResult.success(v.text(), status=v.status)


TOOLS: tuple[Tool, ...] = (
    Tool("tech_decision", "Remember why a technology or library was accepted, rejected or deferred, and when to look again; lists decisions that are due for a re-check so an old "
         "rejection is revisited instead of repeated ('remember we rejected X because ...', 'which decisions should I revisit').", tech_decision, DecisionArgs, RiskLevel.SAFE,
         "memory", timeout=10, risk=_decision_risk),
    Tool("verify_claim", "Independently check a claim that something was done by looking at the real result: a file that was saved (exists, size, recent, expected text, can be reopened) "
         "or a Git operation (branch, latest commit message, files touched, clean tree). Read-only; never returns file contents ('did the save really work', 'check the commit').",
         verify_claim, VerifyArgs, RiskLevel.SAFE, "files", timeout=40),
    Tool("ui_drift", "Check whether a desktop program's interface changed since Jarvis last learned it, and find the likely replacement for a control that moved or was renamed "
         "('did Excel change', 'where did the Save button go'). Replacement is only called SAFE when it is clear; otherwise it is a suggestion. Saving or rolling back a profile "
         "asks first.", ui_drift, DriftArgs, RiskLevel.SAFE, "windows", timeout=40, risk=_drift_risk),
    Tool("place_task", "Recommend which of the user's trusted devices should do a task (PC, phone, other computers) from abilities, memory, GPU, battery, privacy and load, "
         "and say why the others were ruled out. Recommendation only ('which device should run this', 'can the phone do it').", place_task, PlaceArgs, RiskLevel.SAFE, "windows", timeout=20),
    Tool("doc_to_plan", "Read a document's recommendations, requirements and claims and check each against the real Jarvis source, tests and docs; returns gap-first proposals with "
         "files, tests and acceptance criteria. Read-only; the document is treated as untrusted ('what in this paper is missing from Jarvis').", doc_to_plan, DocPlanArgs, RiskLevel.SAFE, "memory", timeout=60),
)
