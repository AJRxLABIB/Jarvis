"""Round 94 tools: ``exec_lab`` (experiments on copies, with an honest isolation level) and ``action_evidence`` (what was independently verified).
Cores: app/core/exec_lab.py, action_evidence.py."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.core import action_evidence as AE, exec_lab as XL
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _store(ctx: ToolContext):
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    from app.memory.settings_store import SettingsStore

    return SettingsStore(ctx.database)


def _root(ctx: ToolContext) -> Path:
    if ctx.paths is None:
        raise ToolError("Needs the data folder.", "no_paths")
    return Path(ctx.paths.data_dir) / "lab"


class LabArgs(ToolArgs):
    action: Literal["status", "plan", "create", "analyze", "sandbox_file", "artifacts", "list", "close"] = Field(
        default="status", description="status = which isolation this PC offers; plan = how an operation would be run (or why not); create = copy a file/folder into a new "
        "experiment; analyze = static check of an experiment (runs nothing); sandbox_file = write a Windows Sandbox file for it; artifacts = files it produced; list; "
        "close = delete the experiment's workspace")
    operation: str = Field(default="", max_length=300, description="plan / create: what you want to do, e.g. 'run the tests of my project', 'check if this installer is safe'")
    path: str = Field(default="", max_length=400, description="create: the file or folder to copy in")
    own_project: bool = Field(default=True, description="plan / create: is this your own code? Someone else's code is treated as untrusted")
    id: str = Field(default="", max_length=20, description="The experiment id (analyze / sandbox_file / artifacts / close)")


def _lab_risk(args: LabArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("create", "sandbox_file", "close") else RiskLevel.SAFE


def _workspace(ctx: ToolContext, reg: XL.Registry, eid: str) -> tuple[XL.Workspace, dict]:
    e = reg.get(eid.strip())
    if e is None:
        raise ToolError("No such experiment. Use list to see them.", "not_found")
    m = XL.Manifest(e["id"], e["purpose"], e["risk"], e["backend"], created=e["created"])
    return XL.Workspace(_root(ctx), m), e


def exec_lab(args: LabArgs, ctx: ToolContext) -> ToolResult:
    backends = XL.detect_backends(*XL.read_host())
    if args.action == "status":
        return ToolResult.success("\n".join(f"{b.name} [{b.isolation}]: {'available' if b.available else 'NOT available'} - {b.reason}" for b in backends),
                                  strong=any(b.available and b.isolation == "strong" for b in backends))
    if args.action == "plan":
        if not args.operation.strip():
            raise ToolError("What do you want to do in the lab?", "missing_operation")
        d = XL.decide(XL.classify(args.operation), backends, args.own_project)
        return ToolResult.success(d.text(), allowed=d.allowed, risk=d.risk, backend=d.backend)
    reg = XL.Registry(_store(ctx))
    if args.action == "list":
        items = reg.all()
        return ToolResult.success("\n".join(f"{e['id']} [{e['state']}] {e['risk']} / {e['backend']}: {e['purpose']}" for e in items[-12:]) or "No experiments.", count=len(items))
    if args.action == "create":
        source = ctx.get_path_policy().resolve(args.path, must_exist=True)
        risk = XL.classify(args.operation or "inspect")
        d = XL.decide(risk, backends, args.own_project)
        backend = d.backend if d.allowed else "static"          # untrusted + no strong isolation: inspection only, and it says so
        ws = XL.Workspace.create(_root(ctx), args.operation or f"inspect {source.name}", d.risk, backend)
        n = ws.add_input(source)
        reg.add(ws.manifest, str(ws.path))
        note = "" if d.allowed else " It can only be INSPECTED here: " + d.reason
        return ToolResult.success(f"Experiment {ws.manifest.id}: copied {n} file(s), left out {len(ws.manifest.skipped)} (secrets, links, caches). Backend: {backend}.{note}",
                                  id=ws.manifest.id, files=n, backend=backend)
    if not args.id.strip():
        raise ToolError("Which experiment id?", "missing_id")
    ws, entry = _workspace(ctx, reg, args.id)
    if args.action == "analyze":
        a = XL.analyze(ws.input)
        return ToolResult.success(XL.describe_analysis(a), files=a["files"], attention=len(a["attention"]), executables=len(a["executables"]))
    if args.action == "artifacts":
        items = XL.Artifacts(ws).listing()
        return ToolResult.success("\n".join(f"{a['name']} ({a['bytes']} bytes, sha256 {a['sha256']})" for a in items[:30]) or "The experiment produced no files.", count=len(items))
    if args.action == "sandbox_file":
        box = next(b for b in backends if b.name == "windows_sandbox")
        if not box.available:
            raise ToolError(f"Windows Sandbox is not available on this PC: {box.reason}.", "backend_unavailable")
        target = ws.path / "experiment.wsb"
        target.write_text(XL.wsb_config(ws), encoding="utf-8")
        return ToolResult.success(f"Wrote {target.name} in the experiment folder. Open it to start the sandbox (networking off, input read-only). Jarvis does not start it.", file=str(target))
    ws.destroy()
    reg.remove(entry["id"])
    return ToolResult.success(f"Experiment {entry['id']} closed and its workspace deleted.")


class EvidenceArgs(ToolArgs):
    action: Literal["recent", "report", "stats", "verifier"] = Field(default="stats", description="stats = how many actions are verified / failed / unverified; recent = latest evidence "
                                                                     "records; report = the verdict for one action; verifier = which independent check fits a kind of action")
    name: str = Field(default="", max_length=120, description="report: the action name; verifier: the kind (file, git, code, screen, phone, setting, install, email ...)")
    days: int = Field(default=7, ge=1, le=365, description="stats: how many days back")


def action_evidence(args: EvidenceArgs, ctx: ToolContext) -> ToolResult:
    if args.action == "verifier":
        name, what, ok = AE.verifier_for(args.name)
        return ToolResult.success(f"{args.name or 'that'}: {name} - {what}.", available=ok)
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    st = AE.Store(ctx.database)
    if args.action == "recent":
        rows = st.recent(20)
        return ToolResult.success(AE.describe(rows), count=len(rows))
    if args.action == "report":
        if not args.name.strip():
            raise ToolError("Which action?", "missing_name")
        rows = st.for_action(args.name.strip())
        v = AE.verdict(rows)
        return ToolResult.success(v.text() + "\n" + AE.describe(rows), status=v.status, green=v.green)
    s = st.stats(time.time() - args.days * 86400)
    total = sum(s.values())
    return ToolResult.success(f"Last {args.days} day(s), {total} checked action(s): " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in s.items())
                              + ". Only 'verified' counts as done; actions with no evidence record at all are not in this count.", **s)


TOOLS: tuple[Tool, ...] = (
    Tool("exec_lab", "The execution laboratory: see which isolation this PC really offers, plan how an operation would run (or why it cannot), copy a file or project into an "
         "experiment workspace (secrets left out), statically analyse it without running anything, list its artifacts. Untrusted code is never run without strong isolation "
         "('is this installer safe', 'investigate my project in the lab').", exec_lab, LabArgs, RiskLevel.SAFE, "dev", timeout=120, risk=_lab_risk),
    Tool("action_evidence", "What Jarvis has independently VERIFIED versus only reported: verdict counts, recent evidence records, the verdict for one action, and which independent "
         "check fits a kind of action ('what did you actually verify', 'how do you check an email was sent').", action_evidence, EvidenceArgs, RiskLevel.SAFE, "system", timeout=15),
)
