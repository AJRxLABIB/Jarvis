"""'Why did you do that?', 'check yourself' and 'repair yourself' (app/core/explain.py, app/core/diagnostics.py)."""

from __future__ import annotations

import sys
from typing import Literal
from pathlib import Path

from pydantic import Field

from app.core import diagnostics, explain
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _source_root() -> Path | None:
    if getattr(sys, "frozen", False):
        return None
    import app

    return Path(app.__file__).resolve().parent


def _current_run_id() -> str | None:
    from app.core import runs

    run = runs.current()
    return run.id if run is not None else None


class ExplainArgs(ToolArgs):
    about: str = Field(default="", max_length=80,
                       description="Optional: a word from the request or action to explain, e.g. 'email', 'delete'")


def explain_action(args: ExplainArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("Explanations need the database (where the audit trail is kept).", "no_database")
    from app.core.runs import RunStore

    store = RunStore(ctx.database)
    run = explain.pick_run(store.recent(30), exclude=_current_run_id(), about=args.about)
    if run is None:
        return ToolResult.success("I haven't done anything recently that needs explaining - I only answered.",
                                  found=False)
    text, steps = explain.explain_run(run, store.trace(run["id"]))
    return ToolResult.success(text, run=run["id"], steps=[s.__dict__ for s in steps])


def diagnose_jarvis(_: NoArgs, ctx: ToolContext) -> ToolResult:
    checks = diagnostics.diagnose(ctx, current_run=_current_run_id(), source_root=_source_root())
    bad = [c for c in checks if c.status != "ok"]
    return ToolResult.success(diagnostics.summary(checks), checks=diagnostics.to_dicts(checks), problems=len(bad))


def repair_jarvis(_: NoArgs, ctx: ToolContext) -> ToolResult:
    done = diagnostics.repair(ctx, current_run=_current_run_id(), source_root=_source_root())
    if not done:
        return ToolResult.success("Nothing needed repairing.", repaired=[])
    return ToolResult.success("Repaired: " + "; ".join(done) + ".", repaired=done)


def pc_health(_: NoArgs, ctx: ToolContext) -> ToolResult:
    """How the PC has been doing (24 h of readings) and what looks wrong, with evidence. Read-only."""
    from app.core import metrics

    if ctx.database is None:
        raise ToolError("The health history needs the database.", "no_database")
    health = ctx.ai.health.summary() if ctx.ai is not None and hasattr(ctx.ai, "health") else {}
    scheduler = getattr(ctx, "scheduler", None)
    workers = scheduler.health() if scheduler is not None and hasattr(scheduler, "health") else None
    text, data = metrics.summary(ctx.database, model_health=health, workers=workers)
    return ToolResult.success(text, **data)


class IntegrationArgs(ToolArgs):
    test: str | None = Field(default=None, max_length=30,
                             description="Optional: a service to smoke-test now (ollama, home_assistant, github, outlook). "
                                         "Omit to just list what is configured and what was last tested")


def integrations_status(args: IntegrationArgs, ctx: ToolContext) -> ToolResult:
    """Which outside services are configured and what their last smoke test found. Listing never contacts them;
    ``test`` sends ONE read-only request to the named service. Neither proves an end-to-end workflow works."""
    from app.core import integrations

    wanted = getattr(args, "test", None)   # older callers pass no arguments at all
    if wanted:
        try:
            result = integrations.run_smoke(ctx, wanted.strip().lower())
        except KeyError:
            raise ToolError(f"I don't know a service called '{wanted}'.", "unknown_service") from None
        text = f"{wanted}:{result['verdict'].replace('_', ' ')} - {result['detail']}. (A smoke test, not an end-to-end test.)"
        return ToolResult.success(text, **result)
    rows = integrations.evaluate_all(ctx)
    return ToolResult.success(integrations.describe(rows), integrations=rows)


class VoiceStatsArgs(ToolArgs):
    days: int = Field(default=14, ge=1, le=60, description="How many days back")


def voice_stats(args: VoiceStatsArgs, ctx: ToolContext) -> ToolResult:
    """How voice has been going: recognition and first-reply times, low-confidence and no-command wakes, by language."""
    from app.core.voice_stats import VoiceStats

    if ctx.database is None:
        raise ToolError("Voice statistics need the database.", "no_database")
    stats = VoiceStats(ctx.database)
    return ToolResult.success(stats.describe(args.days), **stats.report(args.days))


class HealthHistoryArgs(ToolArgs):
    action: Literal["baseline", "delete"] = Field(description="baseline = what is normal for this PC; delete = forget readings")
    older_than_days: int | None = Field(default=None, ge=1, le=365, description="For delete: only readings older than this "
                                                                              "(omit to delete all)")


def health_history(args: HealthHistoryArgs, ctx: ToolContext) -> ToolResult:
    """What is normal for this PC, or forget the recorded health readings. Only CPU, memory, disk and battery numbers
    are ever recorded - no process names, window titles, keystrokes, screenshots or audio."""
    from app.core import metrics

    if ctx.database is None:
        raise ToolError("The health history needs the database.", "no_database")
    store = metrics.MetricsStore(ctx.database)
    if args.action == "delete":
        removed = store.delete(args.older_than_days)
        return ToolResult.success(f"Forgot {removed} health reading(s).", removed=removed)
    rows = store.history(24 * 90)
    base = metrics.baseline(rows)
    if not base:
        return ToolResult.success(f"Not enough history yet to say what is normal ({len(rows)} readings; "
                                  f"{metrics.BASELINE_MIN_SAMPLES} are needed per measure).", readings=len(rows))
    text = "Normal for this PC: " + "; ".join(f"{k.replace('_percent', '')} median {v['median']:.0f}%, usually below "
                                              f"{v['p90']:.0f}% ({v['samples']} readings)" for k, v in base.items()) + "."
    return ToolResult.success(text, baseline=base, readings=len(rows))


class TroubleshootArgs(ToolArgs):
    action: Literal["scan", "preview", "fix", "undo"] = Field(
        description="scan = find setup problems with evidence, cause and fix; preview = what a repair would change; "
                    "fix = do ONE repair (asks first); undo = reverse the .env creation if you have not edited it")
    repair: Literal["create_env", "clear_caches", "close_stuck"] | None = Field(
        default=None, description="For preview / fix / undo: which repair")


def _troubleshoot_risk(args: TroubleshootArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("fix", "undo") else RiskLevel.SAFE


def troubleshoot(args: TroubleshootArgs, ctx: ToolContext) -> ToolResult:
    """Guided troubleshooting. A scan never changes anything and never runs a command; commands are shown for you to run."""
    from app.core import troubleshoot as ts

    if args.action == "scan":
        findings = ts.scan(ctx)
        return ToolResult.success(ts.describe(findings), findings=ts.to_dicts(findings), count=len(findings))
    if not args.repair:
        raise ToolError("Which repair? create_env, clear_caches or close_stuck.", "missing_repair")
    if ctx.paths is None or ctx.database is None:
        raise ToolError("Repairs need the data folder and database.", "no_context")
    if args.action == "preview":
        return ToolResult.success(ts.preview(ctx, args.repair), repair=args.repair)
    if args.action == "undo":
        result = ts.rollback_repair(ctx, args.repair)
        return ToolResult.success(result.get("what") or f"Nothing was undone: {result.get('reason')}.", **result)
    # reaching here means the confirmation (below) was approved for this request
    result = ts.apply(ctx, args.repair, approved=True)
    if not result.get("done"):
        return ToolResult.success(f"Nothing was changed: {result.get('reason')}.", **result)
    verified = "and it is fixed" if result["verified"] else "but the problem is still reported - " + ", ".join(result["still_reported"])
    return ToolResult.success(f"Done: {result['what']}, {verified}. Undo: {result['undo']}.", **result)


class ProgramArgs(ToolArgs):
    action: Literal["report", "delete"] = Field(
        description="report = programs using more than their own usual, and programs heavy before failures; "
                    "delete = forget all recorded program history")


def program_history(args: ProgramArgs, ctx: ToolContext) -> ToolResult:
    """Per-program baselines and crash correlation. Needs PROCESS_HISTORY=true (off by default). Read-only except delete."""
    from app.core import procstats

    if ctx.database is None:
        raise ToolError("Program history needs the database.", "no_database")
    store = procstats.ProcessStore(ctx.database)
    if args.action == "delete":
        removed = store.delete()
        return ToolResult.success(f"Forgot {removed} program reading(s).", removed=removed)
    with ctx.database.connect() as conn:
        have = conn.execute("SELECT COUNT(*) FROM process_samples").fetchone()[0]
    unusual: list = []
    if have:
        try:
            unusual = store.unusual(procstats.sample_top())
        except Exception:
            unusual = []
    failures = store.around_failures() if have else []
    return ToolResult.success(procstats.describe(unusual, failures, bool(have)), readings=have, unusual=unusual,
                              failures=failures[:20])


class TimelineArgs(ToolArgs):
    action: Literal["search", "unverified", "export", "delete_older", "verify"] = Field(
        description="search what Jarvis did; unverified = changes that were never confirmed; export to a file; "
                    "verify = check the activity log has not been edited or cut; "
                    "delete_older permanently removes old records")
    query: str | None = Field(default=None, max_length=80, description="Words to look for (tool, outcome, your request)")
    actor: str | None = Field(default=None, max_length=30, description="voice, typed, phone, automation or Jarvis")
    days: int = Field(default=7, ge=1, le=3650, description="How many days back (delete_older: older than this)")


def _timeline_risk(args: TimelineArgs, ctx: ToolContext) -> RiskLevel:
    return {"delete_older": RiskLevel.DANGEROUS, "export": RiskLevel.MODERATE}.get(args.action, RiskLevel.SAFE)


def activity_timeline(args: TimelineArgs, ctx: ToolContext) -> ToolResult:
    from datetime import timedelta

    from app.core import timeline

    if ctx.database is None:
        raise ToolError("The activity timeline needs the database.", "no_database")
    since = ctx.clock() - timedelta(days=args.days)
    if args.action == "verify":
        from app.core import auditchain

        report = auditchain.verify(ctx.database)
        return ToolResult.success(report.describe(), intact=report.ok, checked=report.checked,
                                  unchained=report.unchained, problems=list(report.problems))
    if args.action == "delete_older":
        removed = timeline.delete_older_than(ctx.database, args.days, ctx.clock())
        return ToolResult.success(f"Permanently deleted {removed} activity record(s) older than {args.days} days.",
                                  removed=removed)
    if args.action == "export":
        base = ctx.paths.data_dir if ctx.paths is not None else Path.home() / ".jarvis"
        path, count = timeline.export(ctx.database, base / "exports", query=args.query, since=since, actor=args.actor,
                                      limit=5000)
        return ToolResult.success(f"Exported {count} activity record(s) to {path.name} in the exports folder.",
                                  path=str(path), count=count)
    items = (timeline.unverified(ctx.database, since) if args.action == "unverified"
             else timeline.entries(ctx.database, args.query, since=since, actor=args.actor))
    head = ("Changes that succeeded but were never confirmed:\n" if args.action == "unverified" and items else "")
    return ToolResult.success(head + timeline.describe(items), items=items[:50], count=len(items))


class TraceArgs(ToolArgs):
    request_id: str | None = Field(default=None, max_length=60,
                                   description="The request (run) ID; omit for the most recent request")


def trace_request(args: TraceArgs, ctx: ToolContext) -> ToolResult:
    """Everything recorded for one request: what ran, what was asked, mission/task changes and events. Read-only."""
    from app.core.runs import RunStore

    if ctx.database is None:
        raise ToolError("The trace needs the database.", "no_database")
    store = RunStore(ctx.database)
    run_id = args.request_id
    if not run_id:
        recent = store.recent(2)
        # the newest row is this very request; the one before it is what the user means by "the last one"
        chosen = recent[1] if len(recent) > 1 else (recent[0] if recent else None)
        if chosen is None:
            return ToolResult.success("No requests are recorded yet.", steps=[])
        run_id = chosen["id"]
    with ctx.database.connect() as conn:
        run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise ToolError(f"There is no recorded request '{run_id}'.", "not_found")
        task_events = conn.execute("SELECT ts, task_id, kind, old_state, new_state, note FROM task_events WHERE run_id = ? "
                                   "ORDER BY id", (run_id,)).fetchall()
        events = conn.execute("SELECT ts, name FROM events WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    steps = store.trace(run_id)
    summary = (f"Request {run_id} ({dict(run)['source']}, {dict(run)['state']}): {len(steps)} recorded action(s), "
               f"{len(task_events)} task change(s), {len(events)} event(s).")
    return ToolResult.success(summary, request=dict(run), actions=[dict(r) for r in steps][:60],
                              task_changes=[dict(r) for r in task_events][:40], events=[dict(r) for r in events][:40])


TOOLS: tuple[Tool, ...] = (
    Tool("activity_timeline", "What Jarvis did and who started it (you by voice/typing/phone, a scheduled automation, or "
         "Jarvis), with whether each result was verified; search, list unverified changes, export, or permanently "
         "delete old records.", activity_timeline, TimelineArgs, RiskLevel.SAFE, "system", timeout=30,
         risk=_timeline_risk,
         confirmation=lambda a, c: (f"Permanently delete activity records older than {a.days} days? This can't be undone."
                                    if a.action == "delete_older" else "Write the activity history to a file in Jarvis's exports folder?")),
    Tool("health_history", "What is normal for this PC (typical CPU and memory), or forget the recorded health readings "
         "(all, or older than N days). Only numbers are recorded: no titles, keystrokes, screenshots or audio (program names "
         "only if you turn PROCESS_HISTORY on; see program_history).",
         health_history, HealthHistoryArgs, RiskLevel.SAFE, "system", timeout=15,
         risk=lambda a, c: RiskLevel.MODERATE if a.action == "delete" else RiskLevel.SAFE,
         confirmation=lambda a, c: "Forget " + (f"health readings older than {a.older_than_days} days?"
                                                if a.older_than_days else "ALL recorded health readings?")),
    Tool("troubleshoot", "Find setup problems (Python version, missing packages, missing models, microphone, missing .env, "
         "database version, services not answering) with evidence, cause and the fix. A scan changes nothing and runs "
         "no command; commands like pip install are shown for you to run. Jarvis itself can only do three small, "
         "previewed repairs after you approve: create a missing .env with the default settings, clear stale caches, close "
         "stuck requests.", troubleshoot, TroubleshootArgs, RiskLevel.SAFE, "system", timeout=30, risk=_troubleshoot_risk,
         confirmation=lambda a, c: ("Undo the .env that Jarvis created (only if you have not edited it)?" if a.action == "undo"
                                    else f"Run the repair '{a.repair}'? Preview it first with: troubleshoot preview {a.repair}.")),
    Tool("program_history","Which programs use more than their own usual, and which program was heaviest just before failed "
         "requests (a correlation, labelled inferred). Needs PROCESS_HISTORY (off by default); records only program names "
         "and CPU/memory numbers. Can also forget that history.", program_history, ProgramArgs, RiskLevel.SAFE, "system",
         timeout=15, risk=lambda a, c: RiskLevel.MODERATE if a.action == "delete" else RiskLevel.SAFE,
         confirmation=lambda a, c: "Forget ALL recorded program history?"),
    Tool("voice_stats","How voice interaction has been going (recognition time, time to first reply, low-confidence "
         "and no-command wakes, per language). Read-only; stores no audio or words.", voice_stats, VoiceStatsArgs,
         RiskLevel.SAFE, "system", timeout=10),
    Tool("integrations", "Which outside services are connected (Home Assistant, the local AI, MCP servers, the phone "
         "remote, Outlook, GitHub, the browser), what each one's last smoke test found, and how to connect the missing ones. "
         "Listing never contacts them; with `test` set it sends one read-only request to that service.",
         integrations_status, IntegrationArgs, RiskLevel.SAFE, "system", timeout=20),
    Tool("trace_request", "Show everything recorded for one request (by its ID, or the last one): actions with risk "
         "and decisions, mission/task state changes and events. Use for 'what exactly happened with that request?'.",
         trace_request, TraceArgs, RiskLevel.SAFE, "system", timeout=15),
    Tool("pc_health", "How the PC has been doing over the last day (CPU, memory, disk, battery) and anything that "
         "looks wrong: sustained high CPU, memory growth, a nearly full disk, failed automations, stuck missions, "
         "failing models - with evidence and what to do. Read-only; never stops anything.", pc_health, NoArgs,
         RiskLevel.SAFE, "system", timeout=20),
    Tool("explain_action", "Explain why Jarvis did (or didn't do, or asked about) its last action, from the "
         "recorded audit trail: which actions ran, their risk, why it asked, what you answered, whether it was "
         "verified. Use for 'why did you do that?', 'why did you ask me?'.", explain_action, ExplainArgs,
         RiskLevel.SAFE, "basic"),
    Tool("diagnose_jarvis", "Check Jarvis's own health: database, disk space, stuck requests/missions, recent "
         "errors, crashes, emergency controls, code caches, local AI. Read-only.", diagnose_jarvis, NoArgs,
         RiskLevel.SAFE, "basic", timeout=30),
    Tool("repair_jarvis", "Fix Jarvis's own bookkeeping after a crash: close requests left running, pause stuck "
         "missions, end idle phone sessions, clear out-of-date code caches. Never touches your files.",
         repair_jarvis, NoArgs, RiskLevel.MODERATE, "basic", timeout=60,
         confirmation=lambda a, c: "Repair Jarvis's own bookkeeping (stuck requests/missions, idle phone "
                                   "sessions, stale code caches)? Your files are not touched."),
)
