"""The personal-intelligence tools: decision records and option comparison, preferences, investigations, research briefs,
long-horizon goal review, the radar's feedback and capability discovery. Rules live in app/core/decisions.py,
preferences.py, gather.py, synthesis.py, longgoals.py, radar.py and capability_gaps.py. Everything is local, and every
tool here only reads or writes small notes on this PC; none can change a permission."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _db(ctx: ToolContext):
    if ctx.database is None:
        raise ToolError("This needs the local database.", "no_database")
    return ctx.database


# ------------------------------------------------------------------ decisions


class ScoreIn(BaseModel):
    value: float | None = Field(default=None, description="The number; leave empty if unknown (an unknown is never treated as 0)")
    basis: Literal["measured", "simulated", "estimated"] = "estimated"
    note: str = Field(default="", max_length=120)


class OptionIn(BaseModel):
    name: str = Field(max_length=60)
    scores: dict[str, ScoreIn] = Field(default_factory=dict, description="criterion -> its number and where it came from")
    reversible: bool = True


class DecisionArgs(ToolArgs):
    action: Literal["record", "list", "show", "correct", "outcome", "delete", "relevant", "compare"] = Field(
        description="record a decision, list them, show one, correct one, note how it turned out, delete one, find earlier "
                    "ones about a topic, or compare options")
    title: str = Field(default="", max_length=200, description="Short title of the decision (record / correct)")
    chosen: str = Field(default="", max_length=300, description="What was chosen (record / correct)")
    alternatives: list[str] = Field(default_factory=list, max_length=8, description="What else was considered")
    criteria: list[str] = Field(default_factory=list, max_length=8, description="Why it was chosen")
    evidence: list[str] = Field(default_factory=list, max_length=8, description="What supports the choice")
    project: str = Field(default="", max_length=100, description="The project it belongs to, if any")
    decision_id: int | None = Field(default=None, ge=1, description="The decision number (show / correct / outcome / delete)")
    outcome: str = Field(default="", max_length=300, description="How it turned out (outcome)")
    status: Literal["active", "superseded", "reversed"] | None = Field(default=None, description="Set with outcome: active, superseded or reversed")
    topic: str = Field(default="", max_length=200, description="For relevant")
    options: list[OptionIn] = Field(default_factory=list, max_length=6, description="For compare")
    weights: dict[str, float] = Field(default_factory=dict, description="Importance per criterion for compare (default 1 each)")
    lower_is_better: list[str] = Field(default_factory=list, description="Criteria where a smaller number is better (latency, memory)")


def decision(args: DecisionArgs, ctx: ToolContext) -> ToolResult:
    from app.core import decisions as D

    if args.action == "compare":
        options = [D.Option(o.name, {k: D.Score(v.value, v.basis, v.note) for k, v in o.scores.items()}, o.reversible)
                   for o in args.options]
        result = D.compare(options, args.weights, {c: False for c in args.lower_is_better})
        return ToolResult.success(result.describe(), verdict=result.verdict, ranking=result.ranking, basis=result.basis,
                                  left_out=result.left_out)
    store = D.DecisionStore(_db(ctx))

    def need_id() -> int:
        if args.decision_id is None:
            raise ToolError("Which decision? Give its number.", "missing_id")
        return args.decision_id

    if args.action == "record":
        record = store.record(args.title, args.chosen, args.alternatives, args.criteria, args.evidence, args.project)
        return ToolResult.success(f"Recorded decision #{record.id}: {record.describe()}", id=record.id)
    if args.action == "list":
        records = store.list(args.project, include_inactive=True)
        return ToolResult.success("\n".join(r.describe() for r in records) or "No decisions recorded yet.",
                                  decisions=[r.id for r in records])
    if args.action == "show":
        record = store.get(need_id())
        return ToolResult.success(record.describe() + (f"\nEvidence: {'; '.join(record.evidence)}" if record.evidence else ""))
    if args.action == "correct":
        changes = {k: v for k, v in {"title": args.title, "chosen": args.chosen, "project": args.project}.items() if v}
        if args.alternatives:
            changes["alternatives"] = args.alternatives
        if args.criteria:
            changes["criteria"] = args.criteria
        record = store.correct(need_id(), **changes)
        return ToolResult.success("Corrected. " + record.describe())
    if args.action == "outcome":
        record = store.set_outcome(need_id(), args.outcome, args.status)
        return ToolResult.success("Noted. " + record.describe())
    if args.action == "delete":
        store.delete(need_id())
        return ToolResult.success(f"Deleted decision #{args.decision_id}.")
    found = store.relevant(args.topic or args.title)
    lines = [f"{r.describe()} ({age} days ago" + (f", {r.status}" if r.status != "active" else "") + ")" for r, age in found]
    return ToolResult.success(("Earlier decisions (context only, not rules):\n" + "\n".join(lines)) if lines else
                              "No earlier decision matches that.", decisions=[r.id for r, _ in found])


def _decision_risk(args: DecisionArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "delete" else RiskLevel.SAFE


# ------------------------------------------------------------------ preferences


class PreferenceArgs(ToolArgs):
    action: Literal["learn", "list", "questions", "confirm", "reject", "remove", "resolve", "adaptation"] = Field(
        description="learn = you stated a preference; list; questions = patterns worth asking you about; confirm / reject a "
                    "noticed pattern; remove one; resolve = what applies now; adaptation = switch learning on or off")
    text: str = Field(default="", max_length=300, description="The preference, in your words (learn)")
    scope: str = Field(default="global", max_length=60, description="global, profile:<name> or project:<name>")
    preference_id: int | None = Field(default=None, ge=1, description="The preference number (confirm / reject / remove)")
    profile: str = Field(default="general", max_length=30, description="For resolve: general, development, study, research, planning or gaming")
    on: bool | None = Field(default=None, description="For adaptation: true = on, false = off")


def preferences(args: PreferenceArgs, ctx: ToolContext) -> ToolResult:
    from app.core import preferences as P

    store = P.PreferenceStore(_db(ctx))

    def need_id() -> int:
        if args.preference_id is None:
            raise ToolError("Which preference? Give its number.", "missing_id")
        return args.preference_id

    if args.action == "learn":
        pref = store.learn(args.text, args.scope)
        return ToolResult.success(f"Remembered (#{pref.id}, {pref.scope}): {pref.text}", id=pref.id)
    if args.action == "list":
        items = store.list()
        lines = [f"#{p.id} [{p.status}, {p.source}, {p.scope}] {p.text}" + (f" (seen {p.seen}x)" if p.source == "observed" else "")
                 for p in items]
        return ToolResult.success("\n".join(lines) or "No preferences yet.", stats=store.stats(), adaptation=store.enabled())
    if args.action == "questions":
        qs = store.questions()
        return ToolResult.success("\n".join(qs) or "Nothing to ask you about.", questions=qs)
    if args.action == "confirm":
        return ToolResult.success(f"Confirmed: {store.confirm(need_id()).text}")
    if args.action == "reject":
        return ToolResult.success(f"I won't use it: {store.reject(need_id()).text}")
    if args.action == "remove":
        store.remove(need_id())
        return ToolResult.success("Removed.")
    if args.action == "adaptation":
        if args.on is None:
            return ToolResult.success("Learning preferences is " + ("on." if store.enabled() else "off."), adaptation=store.enabled())
        store.set_enabled(args.on)
        return ToolResult.success("Learning preferences is now " + ("on." if args.on else "off: only your explicit instructions and the defaults apply."))
    resolved = store.resolve(args.profile)
    return ToolResult.success(resolved.describe(), values={k: v for k, v in resolved.values.items()}, layers=resolved.layers)


def _preference_risk(args: PreferenceArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "remove" else RiskLevel.SAFE


# ------------------------------------------------------------------ investigate / brief / goals / capability / radar


class InvestigateArgs(ToolArgs):
    topic: Literal["model"] = Field(default="model", description="What to investigate: why the local AI model won't answer or load")
    model: str = Field(default="", max_length=100, description="The model name; empty = the configured main model")
    keep_going: bool = Field(default=False, description="Check every cause even after one is found")


async def investigate(args: InvestigateArgs, ctx: ToolContext) -> ToolResult:
    from app.core import gather

    hypotheses, probes = gather.model_failure(ctx, args.model or None)
    report = await gather.investigate("the local AI model isn't answering", hypotheses, probes, gather.Budget(5, 12.0),
                                      stop_at_first=not args.keep_going)
    return ToolResult.success(report.describe(), causes=report.status, supported=[h.key for h in report.supported],
                              missing=report.missing)


class BriefArgs(ToolArgs):
    action: Literal["brief", "gaps", "proposals"] = Field(
        default="brief", description="brief = a sourced local research brief; gaps = what documentation / reasons are missing; "
                                     "proposals = possible new graph links (unconfirmed)")
    question: str = Field(default="", max_length=300, description="What the brief is about (for brief)")


def research_brief(args: BriefArgs, ctx: ToolContext) -> ToolResult:
    from app.core import synthesis

    if args.action == "gaps":
        gaps = synthesis.knowledge_gaps(ctx)
        return ToolResult.success("\n".join(gaps) or "No notable gaps found.", gaps=gaps)
    if args.action == "proposals":
        found = synthesis.propose_relations(ctx)
        lines = [f"{p['from']} -> {p['to']} ({p['because']}) [unconfirmed]" for p in found]
        return ToolResult.success("\n".join(lines) or "No new relationships to propose.", proposals=found)
    if not args.question.strip():
        raise ToolError("What should the brief be about?", "missing_query")
    brief = synthesis.build_brief(ctx, args.question)
    return ToolResult.success(brief.describe(), findings=len(brief.findings), conflicts=len(brief.conflicts))


class GoalReviewArgs(ToolArgs):
    action: Literal["assess", "replan", "review"] = Field(
        default="assess", description="assess = progress, pace and a rough forecast per goal; replan = proposed changes for "
                                      "goals that are behind (nothing is changed); review = this week's summary")
    hours_per_week: float | None = Field(default=None, ge=0, le=100, description="Hours you can really give (an upper bound on pace)")
    minutes_per_task: float | None = Field(default=None, gt=0, le=2000, description="Typical minutes one task takes (with hours_per_week)")


def goal_review(args: GoalReviewArgs, ctx: ToolContext) -> ToolResult:
    from app.core import longgoals as LG
    from app.core.goals import GoalStore

    db = _db(ctx)
    today = ctx.clock().date()
    goals = GoalStore(db).active()
    if not goals:
        return ToolResult.success("You have no active goals.", goals=[])
    if args.action == "review":
        return ToolResult.success(LG.weekly_review(db, goals, today))
    kw = {"hours_per_week": args.hours_per_week, "minutes_per_task": args.minutes_per_task}
    assessments = LG.assess_all(db, goals, today, **kw)
    if args.action == "replan":
        lines = []
        for goal, a in zip(goals, assessments):
            lines += [f"{a.title}: {p}" for p in LG.replan(a, goal, db, today)]
        return ToolResult.success("\n".join(lines) or "Every goal looks on track or has nothing to adjust. Nothing was changed.")
    return ToolResult.success("\n".join(a.describe() for a in assessments),
                              goals=[{"title": a.title, "status": a.status, "done": a.done, "total": a.total} for a in assessments])


class CapabilityArgs(ToolArgs):
    action: Literal["inventory", "gaps"] = Field(
        default="gaps", description="inventory = the tools with their risk, tests and failure rates; gaps = what recurring "
                                    "failures say is really missing, with improvement proposals")
    days: int = Field(default=14, ge=1, le=90, description="How many days of history to look at")


def capability_report(args: CapabilityArgs, ctx: ToolContext) -> ToolResult:
    from app.core import capability_gaps as CG
    from app.tools.catalog import build_default_registry

    db = _db(ctx)
    if args.action == "inventory":
        tests = Path(__file__).resolve().parents[2] / "tests"
        rows = CG.inventory(build_default_registry(), db, tests)
        return ToolResult.success(CG.inventory_summary(rows), tools=len(rows))
    found = CG.proposals(db, args.days)
    if not found:
        return ToolResult.success(f"No recurring failures in the last {args.days} days, so there is nothing to propose.", proposals=[])
    summary = ", ".join(f"{c.replace('_', ' ')} {n}" for c, n in CG.cause_summary(db, args.days).items())
    return ToolResult.success(f"Causes of recurring failures: {summary}.\n\n" + "\n\n".join(p.describe() for p in found[:5]),
                              proposals=[{"tool": p.tool, "cause": p.cause, "count": p.count} for p in found])


class RadarArgs(ToolArgs):
    action: Literal["snooze", "mute", "unmute", "useful", "not_useful", "scan"] = Field(
        description="tell the radar about an alert (by its key) - or scan = show what it would raise right now")
    key: str = Field(default="", max_length=120, description="The alert's key, e.g. resource:ram")
    hours: float = Field(default=24, gt=0, le=720, description="For snooze: how many hours to hold the alert back")


def radar_feedback(args: RadarArgs, ctx: ToolContext) -> ToolResult:
    from app.core import radar

    db = _db(ctx)
    feedback = radar.Feedback(db)
    if args.action == "scan":
        found = radar.signals(ctx, ctx.clock())
        kept, held = feedback.filter(found)
        lines = [f"{c.key}: {c.title} - {c.message}" for c in kept] + [f"{c.key}: held back ({why})" for c, why in held]
        return ToolResult.success("\n".join(lines) or "Nothing needs your attention.", alerts=[c.key for c in kept])
    if not args.key.strip():
        raise ToolError("Which alert? Give its key.", "missing_id")
    key = args.key.strip()
    if args.action == "snooze":
        feedback.snooze(key, args.hours)
        return ToolResult.success(f"Snoozed {key} for {args.hours:g} hours.")
    if args.action == "mute":
        feedback.mute(key)
        return ToolResult.success(f"Muted {key} (unmute any time).")
    if args.action == "unmute":
        feedback.unmute(key)
        return ToolResult.success(f"Unmuted {key}.")
    return ToolResult.success(feedback.rate(key, args.action == "useful"))


def _radar_risk(args: RadarArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.SAFE


class HandoffArgs(ToolArgs):
    action: Literal["create", "inspect", "accept", "revoke", "list"] = Field(
        description="create = package a mission for a paired device; inspect = check a package without using it; accept = "
                    "validate a package and get the resume plan; revoke = cancel unused packages; list = active packages")
    mission_id: str = Field(default="", max_length=64, description="For create: the mission (empty = the unfinished one)")
    device_id: str = Field(default="", max_length=64, description="The paired device (create / accept / revoke)")
    ttl_minutes: int = Field(default=30, ge=1, le=1440, description="For create: how long the package stays valid")
    package: str = Field(default="", max_length=40000, description="For inspect / accept: the handoff JSON a device sent back")
    handoff_id: str = Field(default="", max_length=64, description="For revoke: one package")


def handoff(args: HandoffArgs, ctx: ToolContext) -> ToolResult:
    import json

    from app.core import handoff as H
    from app.core.missions import MissionStore
    from app.remote.mobile import MobileRegistry

    db = _db(ctx)
    registry = MobileRegistry(db)
    missions = MissionStore(db)
    if args.action == "list":
        rows = H.listing(db)
        return ToolResult.success("\n".join(f"{r['id'][:8]}... mission {r['mission_id'][:8]} -> {r['device_id']} (until {r['expires_at'][:16]})" for r in rows)
                                  or "No active handoffs.", handoffs=[r["id"] for r in rows])
    if args.action == "revoke":
        count = H.revoke(db, args.handoff_id, args.device_id)
        return ToolResult.success(f"Revoked {count} handoff(s).", revoked=count)
    if args.action == "create":
        if not args.device_id:
            raise ToolError("Which paired device?", "missing_id")
        mission = missions.get(args.mission_id) if args.mission_id else missions.unfinished()
        if mission is None:
            raise ToolError("There's no unfinished mission to hand over.", "not_found")
        package = H.create(db, mission, args.device_id, args.ttl_minutes, registry=registry)
        return ToolResult.success(f"Handoff for '{mission.title}' ready for {args.device_id}: checkpoint step {package['checkpoint']}, "
                                  f"valid until {package['expires'][:16]}. It carries step titles and states only - no arguments or secrets.",
                                  package=json.dumps(package), id=package["id"])
    if not args.package.strip():
        raise ToolError("Give the handoff package.", "missing_query")
    if args.action == "inspect":
        pkg = H.validate(db, args.package, args.device_id or None, registry=registry)
        return ToolResult.success(f"Valid handoff for '{pkg['title']}': checkpoint step {pkg['checkpoint']}, next: {pkg['next']}, "
                                  f"expires {pkg['expires'][:16]}. Not used yet.")
    outcome = H.accept(db, args.package, missions, args.device_id or None, registry=registry)
    if isinstance(outcome, H.Conflict):
        return ToolResult.failure("conflict", outcome.describe())
    return ToolResult.success(outcome.describe(), mission_id=outcome.mission_id)


def _handoff_risk(args: HandoffArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("create", "accept") else RiskLevel.SAFE


# ------------------------------------------------------------------ anomalies and the experience compiler


def anomalies(_args: NoArgs, ctx: ToolContext) -> ToolResult:
    from app.core import anomaly

    found = anomaly.detect(_db(ctx))
    return ToolResult.success(anomaly.describe(found), anomalies=[a.to_dict() for a in found])


class ExperienceArgs(ToolArgs):
    action: Literal["propose", "save", "dismiss"] = Field(
        default="propose", description="propose = routines you repeat, found in Jarvis's own record of what it did (nothing is "
                                       "saved); save = turn one into a workflow (asks you); dismiss = don't offer one again")
    id: str = Field(default="", max_length=400, description="For save / dismiss: the proposal's id from 'propose'")
    name: str = Field(default="", max_length=60, description="For save: the workflow name (empty = the suggested one)")
    days: int = Field(default=30, ge=7, le=90, description="For propose: how many days of history to look at")


def _existing_sequences(ctx: ToolContext) -> list[list[str]]:
    from app.tools.automation import _store

    try:
        return [[s.tool for s in t.steps if s.type == "tool" and s.tool] for t in _store(ctx).all() if t.is_workflow]
    except ToolError:
        return []


def experience(args: ExperienceArgs, ctx: ToolContext) -> ToolResult:
    from app.automation.tasks import check_name, parse_steps
    from app.automation.triggers import ManualTrigger
    from app.core import experience as X
    from app.tools.automation import _save

    db = _db(ctx)
    found = X.propose(db, days=args.days, existing_steps=_existing_sequences(ctx))
    if args.action == "propose":
        if not found:
            return ToolResult.success(
                f"I don't see a routine you repeat yet (it needs {X.MIN_RUNS} runs on {X.MIN_DAYS} different days, "
                "in the same order, among what I did for you). Nothing was saved.", proposals=[])
        return ToolResult.success("\n".join(f"{i}. {p.describe()}" for i, p in enumerate(found, 1)) +
                                  "\n\nSay 'save routine <number or id>' to turn one into a workflow; nothing is saved until you confirm.",
                                  proposals=[p.to_dict() for p in found])
    chosen = next((p for p in found if p.id == args.id), None)
    if chosen is None and args.id.isdigit() and 1 <= int(args.id) <= len(found):
        chosen = found[int(args.id) - 1]
    if args.action == "dismiss":
        target = chosen.id if chosen else args.id
        if not target:
            raise ToolError("Say which proposal to dismiss (its number or id).", "missing_id")
        X.dismiss(db, target)
        return ToolResult.success("Won't offer that routine again.")
    if chosen is None:
        raise ToolError("I can't find that proposal any more - ask for the proposals again.", "not_found")
    name = check_name(args.name or chosen.name)
    task, replaced = _save(ctx, name, ManualTrigger(), parse_steps(chosen.steps), chosen.parameters or None)
    return ToolResult.success(f"{'Updated' if replaced else 'Saved'} the workflow '{task.name}' with {len(task.steps)} step(s). "
                              f"Run it any time: 'run {task.name}'.", **task.to_dict())


def _experience_risk(args: ExperienceArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "save" else RiskLevel.SAFE


def _experience_question(args: ExperienceArgs, ctx: ToolContext) -> str:
    try:
        from app.core import experience as X

        found = X.propose(_db(ctx), existing_steps=_existing_sequences(ctx))
        p = next((x for x in found if x.id == args.id), None) or (
            found[int(args.id) - 1] if args.id.isdigit() and 1 <= int(args.id) <= len(found) else None)
        if p is not None:
            return f"Save this as a workflow? {p.describe()}"
    except Exception:
        pass
    return "Save this routine as a workflow?"


TOOLS: tuple[Tool, ...] = (
    Tool("handoff", "Move a mission between this PC and a paired phone: create a signed, expiring package (step titles and "
         "states only, no secrets), inspect or accept one a device sends back (it is checked against the mission's real state "
         "and used once; nothing runs until you resume), revoke, or list.", handoff, HandoffArgs, RiskLevel.SAFE, "automation",
         risk=_handoff_risk, confirmation=lambda a, c: ("Hand this mission to your paired device?" if a.action == "create"
                                                         else "Accept this handoff and prepare to resume the mission?")),
    Tool("anomalies", "What looks statistically unusual for you right now: tools that got slower or started failing, PC load, "
         "and spending far above your usual - measured against your own history (median / MAD), with too little history "
         "meaning no verdict.", anomalies, NoArgs, RiskLevel.SAFE, "system", timeout=20),
    Tool("experience", "Find routines you repeat (the same successful tool steps in the same order on different days) from "
         "Jarvis's own action record and offer them as workflows; saving asks you, dismissed ones aren't offered again. Never "
         "reads keyboard, mouse or screen.", experience, ExperienceArgs, RiskLevel.SAFE, "automation", timeout=30,
         risk=_experience_risk, confirmation=_experience_question),
    Tool("decision", "Decision records and option comparison: record what was chosen over what and why, correct or delete it, "
         "note how it turned out, find earlier decisions about a topic (context, not rules), or compare options on measured / "
         "simulated / estimated numbers (unknowns are left out, never treated as zero).", decision, DecisionArgs,
         RiskLevel.SAFE, "personal", risk=_decision_risk, confirmation=lambda a, c: f"Delete decision #{a.decision_id}?"),
    Tool("preferences", "Your preferences for how Jarvis works: save one you state ('keep responses shorter'), list / confirm / "
         "reject / remove them, see what applies in a profile, or switch learning off. Cannot change approvals or security.",
         preferences, PreferenceArgs, RiskLevel.SAFE, "personal", risk=_preference_risk,
         confirmation=lambda a, c: "Remove this preference?"),
    Tool("investigate", "Find out why the local AI model isn't answering by checking the cheapest evidence first (is Ollama "
         "up, is the model installed, is there enough free memory, recent errors) and stopping when a cause is supported.",
         investigate, InvestigateArgs, RiskLevel.SAFE, "system", timeout=30),
    Tool("research_brief", "A sourced research brief from your own memories, decisions, indexed documents and knowledge graph "
         "(offline), marking outdated and conflicting information; also documentation gaps and proposed (unconfirmed) links.",
         research_brief, BriefArgs, RiskLevel.SAFE, "personal", timeout=30),
    Tool("goal_review", "Long-horizon goals: progress from finished tasks, pace from your own history, a rough forecast against "
         "the deadline, proposed replans (nothing is changed), and a weekly review.", goal_review, GoalReviewArgs,
         RiskLevel.SAFE, "automation", timeout=20),
    Tool("capability_report", "What Jarvis is really missing: recurring failures classified by cause (missing tool, permission, "
         "information, model limit, platform...) with improvement proposals, or an inventory of tools with risk, tests and "
         "failure rates.", capability_report, CapabilityArgs, RiskLevel.SAFE, "system", timeout=30),
    Tool("radar_feedback", "Tell the proactive radar about an alert: snooze, mute, unmute, useful / not useful (three 'not "
         "useful' mute it for a week), or scan to see what it would raise now.", radar_feedback, RadarArgs,
         RiskLevel.SAFE, "system", risk=_radar_risk),
)
