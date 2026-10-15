"""Jarvis improving itself: find what keeps failing, propose an exact change, test it on a copy, apply it only
with your approval, roll it back any time. The rules are in app/evolution/__init__.py."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.core import emergency
from app.evolution import analyze, deploy, lab
from app.evolution.proposals import OPEN_STATES, Proposal, ProposalStore, diff_text, fingerprint, validate
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.evolution")
RUNNER = None  # tests put a stand-in here; None = the real CommandExecutor
REBUILD = ("cd D:\\Jarvis; .venv\\Scripts\\activate; powershell -ExecutionPolicy Bypass -File "
           "packaging\\build.ps1 -Install")


def source_dir(ctx: ToolContext) -> Path:
    """Jarvis's own source: JARVIS_SOURCE_DIR, else the folder Jarvis runs from (when not installed)."""
    from app.utils.paths import is_frozen, project_root

    configured = getattr(ctx.settings, "jarvis_source_dir", None)
    folder = Path(configured) if configured else (None if is_frozen() else project_root())
    if folder is None or not (folder / "app" / "__init__.py").is_file():
        raise ToolError("I don't know where my source code is. Set JARVIS_SOURCE_DIR (Settings -> Developer) to "
                        "the folder with run.py, for example D:\\Jarvis.", "no_source")
    return folder


def _folder(ctx: ToolContext) -> Path:
    if ctx.paths is None:
        raise ToolError("There's no data folder to keep proposals in.", "no_data_dir")
    return Path(ctx.paths.data_dir) / "evolution"


def _store(ctx: ToolContext) -> ProposalStore:
    if ctx.database is None:
        raise ToolError("Proposals need the database.", "no_database")
    return ProposalStore(ctx.database)


def _now(ctx: ToolContext) -> str:
    return ctx.clock().isoformat(timespec="seconds")


def _not_switched_off() -> None:
    if emergency.STATE.flags.get("evolution_disabled"):
        raise ToolError("Self-improvement is switched off (emergency controls). Turn it back on in the window.",
                        "emergency")


def _code(ctx: ToolContext, proposal_id: int) -> Proposal:
    proposal = _store(ctx).get(proposal_id)
    if proposal.kind != "code":
        raise ToolError(f"#{proposal.id} is an idea, not a code change. Ask me to draft the change for it first.",
                        "not_code")
    return proposal


# ------------------------------------------------------------------ improve yourself / list / show


def improve_yourself(_: NoArgs, ctx: ToolContext) -> ToolResult:
    store = _store(ctx)
    findings = analyze.failures(ctx.database)
    added = []
    for finding in findings[:10]:
        if store.has_open_insight(finding["key"]):
            continue
        added.append(store.add("insight", analyze.insight_title(finding), finding["meaning"], finding,
                               now=_now(ctx)))
    open_ones = [p for p in store.list(OPEN_STATES, limit=20)]
    if not findings and not open_ones:
        return ToolResult.success("I looked at the last 14 days: nothing keeps failing, so I have no improvement to "
                                  "propose from evidence right now. You can still tell me what to improve.",
                                  findings=0)
    top = "; ".join(p.summary() for p in open_ones[:3])
    return ToolResult.success(
        f"I found {len(findings)} thing(s) that keep failing ({len(added)} new). Top: {top}. Next: I draft an exact "
        "code change for one (you'll see the diff and the risk), test it on a copy of me, and only apply it if the "
        "tests pass and you approve. Nothing has changed yet.", findings=len(findings),
        new=[p.id for p in added], open=[p.id for p in open_ones])


class ListArgs(ToolArgs):
    state: Literal["open", "all", "proposed", "testing", "tested", "failed", "applied", "rejected",
                   "rolled_back"] = Field(default="open", description="Which proposals: open (default), all, or one state")


def list_proposals(args: ListArgs, ctx: ToolContext) -> ToolResult:
    states = None if args.state == "all" else OPEN_STATES if args.state == "open" else (args.state,)
    proposals = _store(ctx).list(states, limit=30)
    if not proposals:
        return ToolResult.success("There are no proposals" + ("" if args.state == "all" else f" ({args.state})") + ".",
                                  proposals=[])
    return ToolResult.success(f"{len(proposals)} proposal(s): " + "; ".join(p.summary() for p in proposals[:8]),
                              proposals=[p.summary() for p in proposals])


class IdArgs(ToolArgs):
    id: int = Field(ge=1, description="The proposal number, e.g. 3 for #3")


def show_proposal(args: IdArgs, ctx: ToolContext) -> ToolResult:
    proposal = _store(ctx).get(args.id)
    parts = [proposal.summary()]
    if proposal.reason:
        parts.append(f"Why: {proposal.reason}")
    if proposal.evidence:
        parts.append("Evidence: " + ", ".join(f"{k}={v}" for k, v in proposal.evidence.items() if k != "key"))
    diff = ""
    if proposal.edits and proposal.state not in ("applied", "rolled_back"):
        try:
            diff = diff_text(source_dir(ctx), proposal.edits)
        except ToolError as exc:
            diff = f"(the change no longer fits the source: {exc})"
    report = proposal.report
    if report:
        tests = report.get("tests", {})
        parts.append(f"Test report: {'PASSED' if report.get('passed') else 'FAILED'} - {tests.get('passed', 0)} "
                     f"passed, {tests.get('failed', 0)} failed; linter {'clean' if report.get('lint_ok') else 'not clean'}"
                     + (f" ({'; '.join(report.get('reasons', []))})" if report.get("reasons") else ""))
    return ToolResult.success("\n".join(parts), diff=diff[:6000], report=report, state=proposal.state)


# ------------------------------------------------------------------ propose


class EditArg(BaseModel):
    path: str = Field(min_length=3, max_length=200, description="File inside Jarvis's source, e.g. app/tools/x.py")
    find: str = Field(default="", max_length=10000,
                      description="Exact existing text to replace (must occur once); empty = create a new file")
    replace: str = Field(default="", max_length=10000, description="The new text")


class ProposeArgs(ToolArgs):
    title: str = Field(min_length=5, max_length=150, description="Short name of the improvement")
    reason: str = Field(min_length=5, max_length=1500, description="Why: the problem it fixes and the evidence")
    edits: list[EditArg] = Field(min_length=1, max_length=12, description="The exact find/replace edits")


def propose_code_change(args: ProposeArgs, ctx: ToolContext) -> ToolResult:
    source = source_dir(ctx)
    edits, risk, facts = validate(source, [e.model_dump() for e in args.edits])
    proposal = _store(ctx).add("code", args.title, args.reason, facts, risk, edits, now=_now(ctx))
    log_event(_log, "proposal_added", id=proposal.id, files=facts["files"], risk=risk)
    return ToolResult.success(
        f"Saved as proposal #{proposal.id}: {args.title} - {facts['changed_lines']} changed line(s) in "
        f"{', '.join(facts['files'])}, risk {risk}. Nothing has changed yet. Say 'test proposal {proposal.id}' to "
        "try it on a copy of me.", id=proposal.id, risk=risk, **facts)


# ------------------------------------------------------------------ test / apply / reject / roll back


def _test_precheck(args: IdArgs, ctx: ToolContext) -> None:
    _not_switched_off()
    proposal = _code(ctx, args.id)
    if proposal.state not in ("proposed", "failed", "tested", "testing"):  # "testing" = an interrupted run
        raise ToolError(f"#{proposal.id} is {proposal.state}; it can't be tested now.", "wrong_state")
    source_dir(ctx)


def test_proposal(args: IdArgs, ctx: ToolContext) -> ToolResult:
    _not_switched_off()
    store, folder, source = _store(ctx), _folder(ctx), source_dir(ctx)
    proposal = _code(ctx, args.id)
    channels = deploy.load_channels(folder)
    baseline = int(channels.get("baseline") or 0)
    from app.security.command_executor import EXECUTOR  # the reviewed runner: fixed argv, no shell

    executor = RUNNER or EXECUTOR
    store.update(proposal.id, state="testing")
    channels["candidate"] = proposal.id
    deploy.save_channels(folder, channels)
    try:
        report = lab.evaluate(proposal, source, folder / "candidates", executor, baseline)
    except ToolError as exc:
        store.update(proposal.id, state="failed", report={"passed": False, "reasons": [str(exc)]})
        raise
    except Exception as exc:
        store.update(proposal.id, state="failed", report={"passed": False, "reasons": [exc.__class__.__name__]})
        raise ToolError(f"Testing #{proposal.id} didn't finish ({exc.__class__.__name__}).", "test_failed") from exc
    state = "tested" if report["passed"] else "failed"
    store.update(proposal.id, state=state, report=report,
                 fingerprint=fingerprint(source, [e.path for e in proposal.edits]))
    tests = report["tests"]
    log_event(_log, "proposal_tested", id=proposal.id, accepted=report["passed"], **tests)
    if report["passed"]:
        return ToolResult.success(f"Proposal #{proposal.id} passed on a copy of me: {tests['passed']} tests passed, "
                                  "0 failed, linter clean. Say 'apply proposal "
                                  f"{proposal.id}' to put it into my real code (I'll back up first).",
                                  accepted=True, **tests)
    return ToolResult.failure("tests_failed", f"Proposal #{proposal.id} did NOT pass: {'; '.join(report['reasons'])}. "
                              "My real code is unchanged.", accepted=False, **tests)


def _apply_precheck(args: IdArgs, ctx: ToolContext) -> None:
    _not_switched_off()
    proposal = _code(ctx, args.id)
    if proposal.state != "tested" or not proposal.report.get("passed"):
        raise ToolError(f"#{proposal.id} hasn't passed its tests ({proposal.state}): test it first.", "not_tested")
    if fingerprint(source_dir(ctx), [e.path for e in proposal.edits]) != proposal.fingerprint:
        raise ToolError(f"My source changed since #{proposal.id} was tested: test it again first.", "stale_test")


def _apply_question(args: IdArgs, ctx: ToolContext) -> str:
    proposal = _store(ctx).get(args.id)
    tests = proposal.report.get("tests", {})
    files = sorted({e.path for e in proposal.edits})
    return (f"Change my own code? Proposal #{proposal.id}: {proposal.title}. Files: {', '.join(files)}. Risk: "
            f"{proposal.risk}. Tested on a copy: {tests.get('passed', 0)} passed, {tests.get('failed', 0)} failed. "
            "The originals are backed up and it can be rolled back.")


def apply_proposal(args: IdArgs, ctx: ToolContext) -> ToolResult:
    _apply_precheck(args, ctx)
    store, source = _store(ctx), source_dir(ctx)
    proposal = store.get(args.id)
    done = deploy.apply(proposal, source, _folder(ctx))
    channels = deploy.load_channels(_folder(ctx))
    channels["baseline"] = proposal.report.get("tests", {}).get("passed", 0)
    deploy.save_channels(_folder(ctx), channels)
    store.update(proposal.id, state="applied", applied_at=_now(ctx), backup=done["backup"])
    log_event(_log, "proposal_applied", id=proposal.id, files=sorted(done["files"]))
    try:
        from app.core import events

        events.BUS.publish(events.JARVIS_UPDATED, message=f"Applied proposal #{proposal.id}: {proposal.title}",
                           proposal=proposal.id)
    except Exception:
        pass
    return ToolResult.success(
        f"Applied #{proposal.id} to my source ({len(done['files'])} file(s)), checked on disk, backup kept. It takes "
        f"effect after a rebuild: {REBUILD}. Say 'roll back proposal {proposal.id}' to undo it.",
        files=sorted(done["files"]), backup=done["backup"])


def reject_proposal(args: IdArgs, ctx: ToolContext) -> ToolResult:
    store = _store(ctx)
    proposal = store.get(args.id)
    if proposal.state in ("applied", "rejected", "rolled_back"):
        raise ToolError(f"#{proposal.id} is {proposal.state}; it can't be rejected.", "wrong_state")
    store.update(proposal.id, state="rejected")
    return ToolResult.success(f"Rejected #{proposal.id}.")


class RollbackArgs(ToolArgs):
    id: int | None = Field(default=None, ge=1, description="The proposal to undo; empty = the last one applied")


def _rollback_target(args: RollbackArgs, ctx: ToolContext) -> Proposal:
    store = _store(ctx)
    if args.id is not None:
        proposal = store.get(args.id)
    else:
        applied = store.list(("applied",), limit=1)
        if not applied:
            raise ToolError("No change of mine is applied, so there's nothing to roll back.", "nothing_applied")
        proposal = applied[0]
    if proposal.state != "applied":
        raise ToolError(f"#{proposal.id} isn't applied ({proposal.state}).", "wrong_state")
    return proposal


def rollback_proposal(args: RollbackArgs, ctx: ToolContext) -> ToolResult:
    proposal = _rollback_target(args, ctx)
    restored = deploy.restore(Path(proposal.backup), source_dir(ctx))
    _store(ctx).update(proposal.id, state="rolled_back")
    log_event(_log, "proposal_rolled_back", id=proposal.id, files=restored)
    return ToolResult.success(f"Rolled back #{proposal.id}: {', '.join(restored)} restored from the backup. Rebuild "
                              f"to use the previous version: {REBUILD}", files=restored)


TOOLS: tuple[Tool, ...] = (
    Tool("improve_yourself", "'Improve yourself': look at what keeps failing (real evidence from the last 14 days) "
         "and list improvement proposals. Changes nothing.", improve_yourself, NoArgs, RiskLevel.SAFE,
         category="evolution", timeout=20),
    Tool("list_proposals", "List Jarvis's improvement proposals and their state.", list_proposals, ListArgs,
         RiskLevel.SAFE, category="evolution"),
    Tool("show_proposal", "Show one improvement proposal: why, evidence, the diff, the test report.", show_proposal,
         IdArgs, RiskLevel.SAFE, category="evolution"),
    Tool("propose_code_change", "Save an exact code change to Jarvis's own source as a proposal (find/replace "
         "edits). It is NOT applied: it must be tested and approved. The security core can't be changed.",
         propose_code_change, ProposeArgs, RiskLevel.SAFE, category="evolution", timeout=20),
    Tool("test_proposal", "Test a proposal on a copy of Jarvis's source (full test suite + linter).", test_proposal,
         IdArgs, RiskLevel.DANGEROUS, category="evolution", timeout=900, precheck=_test_precheck,
         confirmation=lambda a, _: f"Test proposal #{a.id} on a copy of my code? The changed code runs (with your "
         "rights) in the copy only; it takes a few minutes."),
    Tool("apply_proposal", "Apply a TESTED proposal to Jarvis's real source (backup first, verified).",
         apply_proposal, IdArgs, RiskLevel.DANGEROUS, category="evolution", timeout=60, precheck=_apply_precheck,
         confirmation=_apply_question),
    Tool("reject_proposal", "Reject an improvement proposal.", reject_proposal, IdArgs, RiskLevel.SAFE,
         category="evolution"),
    Tool("rollback_proposal", "Undo an applied proposal from its backup (the last one if no number is given).",
         rollback_proposal, RollbackArgs, RiskLevel.MODERATE, category="evolution", timeout=60,
         confirmation=lambda a, _: "Roll back " + (f"proposal #{a.id}" if a.id else "my last self-update") + "?"),
)
