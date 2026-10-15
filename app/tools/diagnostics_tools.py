"""'Why did you do that?', 'check yourself' and 'repair yourself' (app/core/explain.py, app/core/diagnostics.py)."""

from __future__ import annotations

import sys
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


def integrations_status(_: NoArgs, ctx: ToolContext) -> ToolResult:
    """Which outside services are connected and how to connect the rest. Read-only; never contacts them."""
    from app.core import integrations

    rows = integrations.evaluate_all(ctx)
    return ToolResult.success(integrations.describe(rows), integrations=rows)


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
    Tool("integrations", "Which outside services are connected (Home Assistant, the local AI, MCP servers, the phone "
         "remote, Outlook, the browser) and how to connect the missing ones. Read-only; never contacts them.",
         integrations_status, NoArgs, RiskLevel.SAFE, "basic", timeout=10),
    Tool("trace_request", "Show everything recorded for one request (by its ID, or the last one): actions with risk "
         "and decisions, mission/task state changes and events. Use for 'what exactly happened with that request?'.",
         trace_request, TraceArgs, RiskLevel.SAFE, "basic", timeout=15),
    Tool("pc_health", "How the PC has been doing over the last day (CPU, memory, disk, battery) and anything that "
         "looks wrong: sustained high CPU, memory growth, a nearly full disk, failed automations, stuck missions, "
         "failing models - with evidence and what to do. Read-only; never stops anything.", pc_health, NoArgs,
         RiskLevel.SAFE, "basic", timeout=20),
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
