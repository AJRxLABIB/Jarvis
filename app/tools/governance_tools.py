"""Round 79 tools: ``lease`` (mission-scoped capability leases), ``predictions`` (what Jarvis expected vs what happened), ``tech_experience`` (problems, failed
attempts, verified fixes, applicability) and ``opportunities`` (evidence-backed improvements). Cores: app/core/leases.py, predictions.py, tech_experience.py, opportunities.py."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import Field

from app.core import leases as L, opportunities as O, predictions as P, tech_experience as T
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _db(ctx: ToolContext):
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    return ctx.database


class LeaseArgs(ToolArgs):
    action: Literal["list", "show", "grant", "revoke"] = Field(default="list", description="list = all leases; show = one mission's lease; grant = limit a mission to listed tools; revoke = end a lease (the mission's calls are then blocked)")
    mission: str = Field(default="", max_length=60, description="The mission id (show / grant / revoke)")
    tools: str = Field(default="", max_length=600, description="grant: comma-separated tool names the mission may use")
    hours: float = Field(default=8.0, ge=0.05, le=72, description="grant: how long the lease lasts, in hours")
    folder: str = Field(default="", max_length=260, description="grant: optional folder; file/folder arguments outside it are refused")
    network: bool = Field(default=False, description="grant: allow tools that need the internet")
    max_level: int = Field(default=1, ge=0, le=2, description="grant: highest risk level allowed (0 read-only, 1 changes, 2 risky); critical is never allowed")
    max_calls: int = Field(default=100, ge=1, le=5000, description="grant: how many tool calls the mission may make")


def lease(args: LeaseArgs, ctx: ToolContext) -> ToolResult:
    store, now = L.LeaseStore(_db(ctx)), time.time()
    if args.action == "list":
        items = store.all()
        return ToolResult.success("\n".join(L.describe(x, now) for x in items) or "No mission has a lease. Missions without one follow the normal permission rules only.", count=len(items))
    if not args.mission.strip():
        raise ToolError("Which mission? Give its id.", "missing_mission")
    if args.action == "show":
        x = store.get(args.mission)
        return ToolResult.success(L.describe(x, now) if x else f"Mission {args.mission} has no lease (normal permission rules only).")
    if args.action == "revoke":
        return ToolResult.success(f"Lease for mission {args.mission} revoked: its remaining steps will be refused." if store.revoke(args.mission) else "There was no active lease to revoke.")
    names = [t.strip() for t in args.tools.split(",") if t.strip()]
    reg = ctx.get_tool_registry() if hasattr(ctx, "get_tool_registry") else None
    unknown = [n for n in names if reg is not None and reg.get(n) is None]
    if not names or unknown:
        raise ToolError("List the tools the mission may use" + (f"; unknown: {', '.join(unknown)}" if unknown else "") + ".", "bad_tools")
    x = store.grant(args.mission, names, args.hours, args.folder, args.network, args.max_level, args.max_calls)
    return ToolResult.success("Lease granted - " + L.describe(x, now) + ". It only restricts: confirmations and other limits still apply.")


def _lease_level(args: LeaseArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.SAFE if args.action in ("list", "show") else RiskLevel.MODERATE


class PredictArgs(ToolArgs):
    action: Literal["report", "list", "predict", "resolve"] = Field(default="report", description="report = how accurate past predictions were; list = open predictions; predict = write one down before acting; resolve = record what really happened")
    topic: str = Field(default="", max_length=60, description="predict / list: short topic, e.g. 'restart fixes service'")
    claim: str = Field(default="", max_length=300, description="predict: what is expected to happen")
    confidence: float = Field(default=0.6, ge=0, le=1, description="predict: how sure, 0 to 1")
    expect: str = Field(default="", max_length=200, description="predict: what observation would confirm it")
    id: str = Field(default="", max_length=12, description="resolve: the prediction id")
    right: bool = Field(default=False, description="resolve: true if the prediction came true")
    evidence: str = Field(default="", max_length=300, description="resolve: what was actually observed (required)")


def predictions(args: PredictArgs, ctx: ToolContext) -> ToolResult:
    led = P.Ledger(_db(ctx))
    if args.action == "predict":
        if not args.claim.strip() or not args.topic.strip():
            raise ToolError("A prediction needs a topic and a claim.", "missing_claim")
        conf, why = P.adjusted(args.confidence, led.items(topic=args.topic, limit=200))
        p = led.predict(args.topic, args.claim, conf, args.expect)
        return ToolResult.success(f"Prediction {p.id} recorded at {conf:.0%} ({why}). It counts as nothing until its real outcome is recorded.", id=p.id, confidence=conf)
    if args.action == "resolve":
        if not args.evidence.strip():
            raise ToolError("Say what was actually observed - an outcome without evidence is not recorded.", "missing_evidence")
        if not led.resolve(args.id, args.right, args.evidence):
            raise ToolError("No open prediction with that id.", "not_found")
        return ToolResult.success(f"Recorded: prediction {args.id} was {'right' if args.right else 'WRONG'}." + ("" if args.right else " The miss is kept and lowers future confidence on this topic."))
    if args.action == "list":
        items = led.items(open_only=True, topic=args.topic)
        return ToolResult.success("\n".join(f"{p.id} [{p.topic}] {p.confidence:.0%}: {p.claim}" for p in items) or "No open predictions.", count=len(items))
    return ToolResult.success(P.describe(P.calibration(led.items(limit=500))))


class ExperienceArgs(ToolArgs):
    action: Literal["recall", "add", "list", "delete", "suggest"] = Field(default="recall", description="recall = find similar past problems and whether their fix applies now; add = record a solved problem; list; delete; suggest = candidates found in the action log (a call that failed, then worked with other arguments)")
    problem: str = Field(default="", max_length=300, description="recall / add: the problem in plain words")
    error: str = Field(default="", max_length=500, description="recall / add: the error text, if any")
    environment: str = Field(default="", max_length=300, description="recall / add: name=value pairs, e.g. 'os=windows 11, mysql=8.0, python=3.14'")
    failed: str = Field(default="", max_length=600, description="add: attempts that did NOT work, separated by ';'")
    solution: str = Field(default="", max_length=500, description="add: what solved it")
    verification: str = Field(default="", max_length=300, description="add: how it was confirmed fixed (leave empty if it was not verified)")
    id: str = Field(default="", max_length=12, description="delete: the record id")


def tech_experience(args: ExperienceArgs, ctx: ToolContext) -> ToolResult:
    store = T.Store(_db(ctx))
    if args.action == "add":
        if not args.problem.strip() or not args.solution.strip():
            raise ToolError("Give the problem and what solved it.", "missing_fields")
        e = store.add(args.problem, args.solution, args.error, T.parse_env(args.environment), args.failed.split(";"), args.verification)
        return ToolResult.success(f"Saved experience {e.id}." + ("" if e.verification else " No verification was given, so it is kept as an UNVERIFIED fix."), id=e.id)
    if args.action == "delete":
        return ToolResult.success("Deleted." if store.delete(args.id) else "No record with that id.")
    if args.action == "suggest":
        cands = T.mine(T.load_audit(ctx.database))
        text = "\n".join(f"- {c['problem']}\n  {c['solution']}" for c in cands) or "No candidates in the action log."
        return ToolResult.success(text + ("\nNothing was saved. Say which one to remember and I will record it." if cands else ""), count=len(cands))
    if args.action == "list":
        items = store.all(30)
        return ToolResult.success("\n".join(f"{e.id}: {e.problem} -> {e.solution}" + ("" if e.verification else " (unverified)") for e in items) or "No experiences recorded yet.", count=len(items))
    if not args.problem.strip() and not args.error.strip():
        raise ToolError("Describe the problem or paste the error.", "missing_problem")
    ms = store.recall(args.problem, args.error, T.parse_env(args.environment))
    return ToolResult.success(T.describe(ms), matches=len(ms), applies=sum(m.verdict == "applies" for m in ms))


def _exp_level(args: ExperienceArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.SAFE if args.action in ("recall", "list") else RiskLevel.MODERATE


class OppArgs(ToolArgs):
    action: Literal["list", "dismiss", "restore"] = Field(default="list", description="list = current opportunities; dismiss = hide one for good; restore = show it again")
    id: str = Field(default="", max_length=120, description="dismiss / restore: the opportunity id (starts with 'opp:')")


def opportunities(args: OppArgs, ctx: ToolContext) -> ToolResult:
    from app.core.radar import Feedback

    fb = Feedback(_db(ctx))
    if args.action in ("dismiss", "restore"):
        if not args.id.startswith("opp:"):
            raise ToolError("Give the opportunity id (it starts with 'opp:').", "bad_id")
        (fb.mute if args.action == "dismiss" else fb.unmute)(args.id)
        return ToolResult.success("Dismissed - it will not be suggested again." if args.action == "dismiss" else "Restored.")
    items, failed = O.collect(ctx.database)
    shown, hidden = O.rank(items, fb.blocked)
    return ToolResult.success(O.describe(shown, hidden, failed), count=len(shown))


class IdleArgs(ToolArgs):
    action: Literal["status", "run_now"] = Field(default="status", description="status = what idle work actually ran; run_now = run the due housekeeping jobs now")


def idle_work(args: IdleArgs, ctx: ToolContext) -> ToolResult:
    from app.core import idle_work as I
    from app.memory.settings_store import SettingsStore

    store = SettingsStore(_db(ctx))
    enabled = bool(getattr(ctx.settings, "idle_work_enabled", True)) if getattr(ctx, "settings", None) is not None else True
    if args.action == "run_now":
        done = I.IdleWorker(ctx.database, store).run_due()
        return ToolResult.success("\n".join(f"{d['job']}: {'ok' if d['ok'] else 'FAILED'} - {d['result']}" for d in done) or "Nothing is due right now.", ran=len(done))
    return ToolResult.success(I.describe(store.get(I.LOG_KEY, []) or [], time.time(), enabled))


class TraceExportArgs(ToolArgs):
    request_id: str = Field(default="", max_length=80, description="The request id to export; empty = the previous request")
    include_arguments: bool = Field(default=False, description="Also write the request text and tool arguments (secret-looking values are replaced). Off by default.")


def trace_export(args: TraceExportArgs, ctx: ToolContext) -> ToolResult:
    from pathlib import Path

    from app.core import otel_export
    from app.core.runs import RunStore

    db = _db(ctx)
    run_id = args.request_id
    if not run_id:
        recent = RunStore(db).recent(2)
        if not recent:
            raise ToolError("No requests are recorded yet.", "not_found")
        run_id = (recent[1] if len(recent) > 1 else recent[0])["id"]
    base = ctx.paths.data_dir if getattr(ctx, "paths", None) is not None else Path.home() / ".jarvis"
    try:
        path, spans = otel_export.export(db, run_id, base / "exports", args.include_arguments)
    except LookupError as exc:
        raise ToolError(str(exc), "not_found")
    return ToolResult.success(f"Wrote {spans} span(s) for request {run_id} to {path.name} in the exports folder (OpenTelemetry OTLP/JSON). "
                              + ("Arguments included, secrets replaced." if args.include_arguments else "Arguments and request text left out.") + " Nothing was sent anywhere.", path=str(path), spans=spans)


class DiscoverArgs(ToolArgs):
    app: str = Field(min_length=1, max_length=60, description="The program to look at, e.g. 'excel', 'git', 'spotify', 'code'")


def discover_integration(args: DiscoverArgs, ctx: ToolContext) -> ToolResult:
    from app.core import integration_discovery as D

    opts = D.discover(args.app)
    return ToolResult.success(D.describe(args.app, opts), options=[o.route for o in opts])


class MergeArgs(ToolArgs):
    action: Literal["list", "scan", "apply", "reject", "undo"] = Field(default="list", description="list = open merge proposals; scan = look for near-identical memories now (rule-based wording); "
                                                                                                "apply = merge one; reject = never propose that pair again; undo = reverse an applied merge")
    id: str = Field(default="", max_length=8, description="apply / reject / undo: the proposal id (the code in brackets)")


def memory_merge(args: MergeArgs, ctx: ToolContext) -> ToolResult:
    from app.core import memory_consolidation as MC
    from app.memory.manager import MemoryManager
    from app.memory.settings_store import SettingsStore

    db = _db(ctx)
    props, manager = MC.Proposals(SettingsStore(db)), MemoryManager(db)
    if args.action == "list":
        return ToolResult.success(MC.describe(props.open()), count=len(props.open()))
    if args.action == "scan":
        added = MC.scan(manager, props, None, limit=5)
        return ToolResult.success(f"{added} new proposal(s).\n" + MC.describe(props.open()), added=added)
    if not args.id.strip():
        raise ToolError("Which proposal? Give its id.", "missing_id")
    try:
        if args.action == "apply":
            return ToolResult.success(MC.apply(manager, props, args.id))
        if args.action == "undo":
            return ToolResult.success(MC.undo(manager, props, args.id))
    except LookupError as exc:
        raise ToolError(str(exc).capitalize() + ".", "not_found")
    except ValueError as exc:
        raise ToolError(str(exc).capitalize() + ".", "stale")
    if props.get(args.id) is None:
        raise ToolError("No proposal with that id.", "not_found")
    props.set_status(args.id, "rejected")
    return ToolResult.success("Rejected - that pair will not be proposed again.")


class BreakerArgs(ToolArgs):
    action: Literal["status", "reset"] = Field(default="status", description="status = which online services are paused after repeated failures; reset = let them be tried again now")
    tool: str = Field(default="", max_length=60, description="reset: one tool name (empty = all)")


def service_breakers(args: BreakerArgs, ctx: ToolContext) -> ToolResult:
    from app.core import circuit

    if args.action == "reset":
        return ToolResult.success(f"Reset {circuit.BREAKERS.reset(args.tool.strip() or None)} paused service(s); they will be tried again.")
    rows = circuit.BREAKERS.snapshot()
    return ToolResult.success(circuit.describe(rows), paused=[r["tool"] for r in rows if r["paused_s"]])


class AttributionArgs(ToolArgs):
    action: Literal["who", "setup"] = Field(default="who", description="who = which program changed files (from Windows' own records); setup = how to switch that recording on")
    folder: str = Field(default="", max_length=260, description="who: only changes under this folder or file path (empty = everything recorded)")


def file_attribution(args: AttributionArgs, ctx: ToolContext) -> ToolResult:
    from app.core import file_attribution as FA, pc_checkup

    if args.action == "setup":
        return ToolResult.success(FA.SETUP)
    changes, notes = FA.read(pc_checkup.run_powershell, pc_checkup.EVENT_SCRIPTS)
    return ToolResult.success(FA.describe(changes, notes, args.folder.strip()), changes=len(changes), notes=notes)


class MissionReportArgs(ToolArgs):
    action: Literal["report", "analytics"] = Field(default="report", description="report = what failed, why, what succeeded, what remains and whether a retry is safe, for one mission; "
                                                                                 "analytics = completion rate, typical duration and the steps that fail most, across missions")
    mission: str = Field(default="", max_length=60, description="report: the mission id (empty = the most recent one)")


def mission_report(args: MissionReportArgs, ctx: ToolContext) -> ToolResult:
    from app.core import mission_report as MR
    from app.core.missions import MissionStore

    db = _db(ctx)
    if args.action == "analytics":
        a = MR.analytics(db)
        return ToolResult.success(MR.describe_analytics(a), **{k: v for k, v in a.items() if k != "failing_tools"})
    store = MissionStore(db)
    try:
        mission = store.get(args.mission.strip()) if args.mission.strip() else (store.recent(1) or [None])[0]
    except Exception:
        mission = None
    if mission is None:
        raise ToolError("There is no such mission." if args.mission.strip() else "No missions yet.", "not_found")
    reg = ctx.get_tool_registry() if hasattr(ctx, "get_tool_registry") else None
    r = MR.report(mission, reg)
    return ToolResult.success(MR.describe(r), failed=len(r["failed"]), remaining=len(r["remaining"]), retry_safe=[f["retry_safe"] for f in r["failed"]])


class MoveCheckArgs(ToolArgs):
    folder: str = Field(min_length=1, max_length=260, description="The project folder to check")
    destination: str = Field(default="", max_length=260, description="Where it would move to (only mentioned in the answer; nothing is moved)")


def project_move_check(args: MoveCheckArgs, ctx: ToolContext) -> ToolResult:
    from app.core import project_move as PM

    try:
        rep = PM.check(args.folder, PM.running_processes)
    except FileNotFoundError as exc:
        raise ToolError(str(exc).capitalize() + ".", "not_found") from exc
    return ToolResult.success(PM.describe(rep, args.destination.strip()), findings=len(rep.findings), kinds=sorted({f.kind for f in rep.findings}), files_read=rep.files_read)


class ApiLeadArgs(ToolArgs):
    app: str = Field(min_length=1, max_length=60, description="The program or service to look up, e.g. 'spotify', 'notion', 'obs studio'")


async def find_app_api(args: ApiLeadArgs, ctx: ToolContext) -> ToolResult:
    """Web leads for a program's official API / SDK. Leads only: titles and links from a search, not verified, not instructions."""
    from app.tools.browser import SearchArgs, UNTRUSTED_NOTE, web_search

    res = await web_search(SearchArgs(query=f"{args.app} official API SDK developer documentation", limit=5), ctx)
    results = (res.data or {}).get("results", []) if hasattr(res, "data") else []
    if not results:
        return ToolResult.success(f"No web results about an API for '{args.app}'. That does not prove there is none.", leads=[])
    lines = [f"- {r.get('title', '')[:90]}: {r.get('url', '')}" for r in results]
    return ToolResult.success(f"Web leads for '{args.app}' (not verified - check that a page is the vendor's own, its licence and how it signs in before anything is built):\n"
                              + "\n".join(lines), leads=results, note=UNTRUSTED_NOTE)


TOOLS: tuple[Tool, ...] = (
    Tool("memory_merge", "Review-first memory consolidation: list proposals to merge two memories that say nearly the same thing, apply one (the second memory is switched off, not deleted), "
         "reject it, or undo an applied merge ('any duplicate memories', 'merge those two memories').", memory_merge, MergeArgs, RiskLevel.SAFE, "memory", timeout=30,
         risk=lambda a, c: RiskLevel.MODERATE if a.action in ("apply", "undo") else RiskLevel.SAFE,
         confirmation=lambda a, c: f"{'Merge' if a.action == 'apply' else 'Undo the merge of'} the two memories in proposal {a.id}?"),
    Tool("service_breakers", "Which online services Jarvis has paused after repeated time-outs or connection failures, and reset them ('why is web search paused', 'try the "
         "services again').", service_breakers, BreakerArgs, RiskLevel.SAFE, "system", timeout=5),
    Tool("file_attribution", "Which PROGRAM changed, created or deleted files, from Windows' own audit / Sysmon records when the user has switched them on, or how to switch them on "
         "('which program changed files in my Documents', 'who deleted that file'). Read-only; paths and program names only.", file_attribution, AttributionArgs, RiskLevel.SAFE,
         "system", timeout=40),
    Tool("mission_report", "After a mission stops: what failed and the probable cause, which steps succeeded, what remains, whether a retry is safe or needs a check first, and another "
         "way forward; or statistics across missions ('why did the mission fail', 'can I retry it', 'how reliable are my missions').", mission_report, MissionReportArgs,
         RiskLevel.SAFE, "automation", timeout=15),
    Tool("project_move_check", "What would break if a project folder moved (e.g. from C: to D:): a static check for a virtual environment, files that contain the folder's own path, "
         "Git pointer files, links and programs running from it. Nothing is moved or run.", project_move_check, MoveCheckArgs, RiskLevel.SAFE, "dev", timeout=60),
    Tool("find_app_api", "Search the web for a program's official API or SDK documentation and return the links as unverified leads ('does Spotify have an API', 'find the Notion SDK'). "
         "Use discover_integration for what is available on this PC.", find_app_api, ApiLeadArgs, RiskLevel.SAFE, category="web", needs_network=True, timeout=30),
    Tool("idle_work", "What Jarvis's idle-time housekeeping actually did while the user was away (with real results), or run the due jobs now ('what did you do while I was away', "
         "'run idle work').", idle_work, IdleArgs, RiskLevel.SAFE, "system", timeout=60),
    Tool("trace_export", "Export one request's recorded trace as an OpenTelemetry (OTLP/JSON) file in Jarvis's exports folder, for an external trace viewer. Local file only; arguments left "
         "out unless asked.", trace_export, TraceExportArgs, RiskLevel.MODERATE, "system", timeout=20,
         confirmation=lambda a, c: "Write this request's trace to a file in Jarvis's exports folder" + (" INCLUDING the request text and tool arguments?" if a.include_arguments else "?")),
    Tool("discover_integration", "Find how a program on this PC could be driven: an existing Jarvis tool, a command-line program, COM automation, a URL scheme, or UI Automation - ranked, with "
         "what each can and cannot do ('how could you integrate with Spotify', 'can you control git'). Read-only; nothing is installed or generated.", discover_integration, DiscoverArgs,
         RiskLevel.SAFE, "windows", timeout=10),
    Tool("lease", "Mission-scoped permission leases: limit a mission to listed tools, a folder, a time and a number of calls; show or revoke a lease ('limit this mission to read-only tools', "
         "'revoke the mission's permissions'). A lease only restricts; confirmations still apply.", lease, LeaseArgs, RiskLevel.SAFE, "system", timeout=10, risk=_lease_level),
    Tool("predictions", "Jarvis's own prediction record: write down what is expected before an action, record what really happened, and report how accurate past predictions were "
         "('how good are your predictions', 'were you right about the restart').", predictions, PredictArgs, RiskLevel.SAFE, "memory", timeout=10),
    Tool("tech_experience", "Technical problem memory: find a similar past problem with what failed, what worked and whether it applies to the current environment; record a solved problem "
         "('have we seen this error before', 'remember how we fixed MySQL').", tech_experience, ExperienceArgs, RiskLevel.SAFE, "memory", timeout=10, risk=_exp_level),
    Tool("opportunities", "Evidence-backed improvement opportunities from Jarvis's own records: repeated manual work, tools that keep failing, paused missions, goals without a plan, overdue promises "
         "('what could be improved', 'any opportunities').", opportunities, OppArgs, RiskLevel.SAFE, "memory", timeout=30),
)
