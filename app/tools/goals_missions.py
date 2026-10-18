"""Goals (what you're working towards) and missions (multi-step tasks with a plan). Rules: app/core/goals.py
and app/core/missions.py."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.core.goals import GoalStore, parse_deadline
from app.core.missions import MAX_STEPS, MissionStore, approve_checkpoint, rollback, run, simulate, validate_plan
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _db(ctx: ToolContext):
    if ctx.database is None:
        raise ToolError("Goals and missions need the database.", "no_database")
    return ctx.database


# ------------------------------------------------------------------ goals


class GoalArgs(ToolArgs):
    title: str = Field(min_length=2, max_length=200, description="The goal, e.g. 'finish the TMS project'")
    deadline: str | None = Field(default=None, max_length=40,
                                 description="When: 'this month', 'by Friday', '15 October', 'in 2 weeks', YYYY-MM-DD")
    priority: int = Field(default=2, ge=1, le=3, description="1 = high, 2 = normal, 3 = low")
    why: str = Field(default="", max_length=300, description="Why it matters, only if the user said")
    tasks: list[str] = Field(default_factory=list, max_length=15,
                             description="The steps towards it, if the user gave them or asked you to plan them")


def set_goal(args: GoalArgs, ctx: ToolContext) -> ToolResult:
    deadline = parse_deadline(args.deadline, ctx.clock().date())
    tasks = [" ".join(t.split())[:200] for t in args.tasks if t and t.strip()]
    goal = GoalStore(_db(ctx), ctx.clock).add(" ".join(args.title.split()), deadline, args.priority, args.why.strip(),
                                              tasks)
    when = f", due {deadline}" if deadline else ""
    steps = f" with {len(tasks)} task(s)" if tasks else " (say 'add a task to my goal: ...' to plan it)"
    return ToolResult.success(f"New goal: {goal.title}{when}{steps}.", goal_id=goal.id, deadline=deadline)


def list_goals(_: NoArgs, ctx: ToolContext) -> ToolResult:
    goals = GoalStore(_db(ctx), ctx.clock).active()
    if not goals:
        return ToolResult.success("You have no active goals.", goals=[])
    today = ctx.clock().date()
    return ToolResult.success(" ".join(f"{i}. {g.describe(today)}." for i, g in enumerate(goals, start=1)),
                              goals=[{"title": g.title, "progress": g.progress, "deadline": g.deadline,
                                      "tasks": g.tasks} for g in goals])


class UpdateGoalArgs(ToolArgs):
    goal: str | None = Field(default=None, max_length=200, description="Which goal (a word from it, or its number)")
    action: Literal["add_task", "complete_task", "progress", "done", "drop"] = Field(description="What to change")
    text: str | None = Field(default=None, max_length=200, description="The task (add_task / complete_task)")
    progress: int | None = Field(default=None, ge=0, le=100, description="Percent, for action 'progress'")


def update_goal(args: UpdateGoalArgs, ctx: ToolContext) -> ToolResult:
    store = GoalStore(_db(ctx), ctx.clock)
    goal = store.find(args.goal)
    if args.action == "add_task":
        if not args.text or not args.text.strip():
            raise ToolError("Which task?", "missing_text")
        goal = store.add_task(goal, " ".join(args.text.split()))
        return ToolResult.success(f"Added to '{goal.title}'. It's at {goal.progress}%.", progress=goal.progress)
    if args.action == "complete_task":
        goal, done = store.complete_task(goal, args.text or "")
        more = [t["text"] for t in goal.tasks if not t["done"]]
        tail = f" Next: {more[0]}." if more else " That was the last task - say 'goal done' to close it."
        return ToolResult.success(f"Done: {done}. '{goal.title}' is at {goal.progress}%.{tail}", progress=goal.progress)
    if args.action == "progress":
        if args.progress is None:
            raise ToolError("How far along, in percent?", "missing_progress")
        goal = store.set_progress(goal, args.progress)
        return ToolResult.success(f"'{goal.title}' is at {goal.progress}%.", progress=goal.progress)
    goal = store.set_status(goal, "done" if args.action == "done" else "dropped")
    return ToolResult.success(f"Goal {'completed' if args.action == 'done' else 'dropped'}: {goal.title}.")


# ------------------------------------------------------------------ missions


class MissionStep(ToolArgs):
    title: str = Field(min_length=1, max_length=120, description="What this step does, in a few words")
    tool: str = Field(min_length=1, max_length=64, description="The tool to call")
    arguments: dict[str, Any] = Field(default_factory=dict, description="The tool's arguments")
    depends_on: list[int] = Field(default_factory=list, max_length=MAX_STEPS,
                                  description="Numbers (1-based) of earlier steps that must succeed first")
    milestone: str = Field(default="", max_length=80, description="Milestone this step belongs to, e.g. 'Research'")
    checkpoint: bool = Field(default=False, description="true = pause for the user's go-ahead before this step")


class MissionArgs(ToolArgs):
    title: str = Field(min_length=2, max_length=200, description="The mission, e.g. 'prepare tomorrow's presentation'")
    steps: list[MissionStep] = Field(min_length=1, max_length=MAX_STEPS, description="The plan: tool calls in order")
    goal: str | None = Field(default=None, max_length=200, description="A goal this mission belongs to, if any")
    dry_run: bool = Field(default=False, description="true = only show the plan and what it would do")


def _plan(args: MissionArgs, ctx: ToolContext):
    executor = ctx.executor
    if executor is None:
        raise ToolError("Missions need the assistant's executor.", "unavailable")
    steps = validate_plan([s.model_dump() for s in args.steps], executor.registry, ctx)
    highest, summary = simulate(steps, executor.registry, executor.permissions, ctx)
    return steps, highest, summary


def _mission_risk(args: MissionArgs, ctx: ToolContext) -> RiskLevel:
    if args.dry_run:
        return RiskLevel.SAFE
    _, highest, _ = _plan(args, ctx)
    return RiskLevel.DANGEROUS if highest >= RiskLevel.MODERATE else RiskLevel.SAFE  # any change: approve the plan


def _mission_question(args: MissionArgs, ctx: ToolContext) -> str:
    try:
        _, _, summary = _plan(args, ctx)
    except ToolError as exc:
        return f"Start the mission '{args.title}'? ({exc})"
    return f"Mission '{args.title}' - {summary} Approve this plan?"


async def start_mission(args: MissionArgs, ctx: ToolContext) -> ToolResult:
    from app.core import runs

    steps, highest, summary = _plan(args, ctx)
    goal_id = GoalStore(_db(ctx), ctx.clock).find(args.goal).id if args.goal else None
    current = runs.current()
    store = MissionStore(_db(ctx))
    mission = store.create(args.title, steps, goal_id, current.id if current else None)
    if args.dry_run:
        return ToolResult.success(f"Simulation (nothing done yet) - {summary} Say 'run the mission' to do it.",
                                  mission=mission.id, simulation=summary)
    outcome = await run(mission, store, ctx.executor, approved=highest >= RiskLevel.MODERATE)
    data = {"mission": mission.id, "progress": outcome.mission.progress()}
    return ToolResult.success(outcome.message, **data) if outcome.ok else \
        ToolResult.failure("mission_paused", outcome.message, **data)


class ResumeArgs(ToolArgs):
    skip_failed: bool = Field(default=False, description="true = skip the step that failed and go on")


def _unfinished(ctx: ToolContext):
    mission = MissionStore(_db(ctx)).unfinished()
    if mission is None:
        raise ToolError("There's no unfinished mission.", "no_mission")
    return mission


def _resume_risk(args: ResumeArgs, ctx: ToolContext) -> RiskLevel:
    mission = _unfinished(ctx)
    remaining = [s for s in mission.steps if s.state not in ("done", "skipped", "undone")]
    if not remaining or ctx.executor is None:
        return RiskLevel.SAFE
    highest, _ = simulate(remaining, ctx.executor.registry, ctx.executor.permissions, ctx)
    return RiskLevel.DANGEROUS if highest >= RiskLevel.MODERATE else RiskLevel.SAFE


def _resume_question(args: ResumeArgs, ctx: ToolContext) -> str:
    mission = _unfinished(ctx)
    remaining = [s for s in mission.steps if s.state not in ("done", "skipped", "undone")]
    _, summary = simulate(remaining, ctx.executor.registry, ctx.executor.permissions, ctx)
    return f"Continue '{mission.title}' ({mission.progress()} done) - remaining {summary} Approve?"


async def resume_mission(args: ResumeArgs, ctx: ToolContext) -> ToolResult:
    mission = _unfinished(ctx)
    store = MissionStore(_db(ctx))
    approve_checkpoint(mission, store)  # 'continue' at a checkpoint is the go-ahead (this call was confirmed)
    remaining = [s for s in mission.steps if s.state not in ("done", "skipped", "undone")]
    highest, _ = simulate(remaining, ctx.executor.registry, ctx.executor.permissions, ctx)
    outcome = await run(mission, store, ctx.executor, approved=highest >= RiskLevel.MODERATE,
                        skip_failed=args.skip_failed)
    data = {"mission": mission.id, "progress": outcome.mission.progress()}
    return ToolResult.success(outcome.message, **data) if outcome.ok else \
        ToolResult.failure("mission_paused", outcome.message, **data)


def cancel_mission(_: NoArgs, ctx: ToolContext) -> ToolResult:
    store = MissionStore(_db(ctx))
    mission = _unfinished(ctx)
    if mission.state in ("running", "verifying"):        # running right now: it stops at the next step boundary
        from app.core import missions as _missions

        _missions.request(mission.id, "cancel")
        return ToolResult.success(f"Cancelling '{mission.title}' before its next step (the step running now finishes "
                                  "first).", pending=True)
    store.set_state(mission, "cancelled", "cancelled by you")
    return ToolResult.success(f"Cancelled '{mission.title}' ({mission.progress()} steps had been done; they stay done).")


def mission_status(_: NoArgs, ctx: ToolContext) -> ToolResult:
    missions = MissionStore(_db(ctx)).recent(5)
    if not missions:
        return ToolResult.success("No missions yet.", missions=[])
    parts = []
    for m in missions:
        extra = f" - {m.note}" if m.note else ""
        stones = m.milestones()
        if stones:
            extra += " (milestones: " + ", ".join(f"{name} {done}/{total}" for name, done, total in stones) + ")"
        parts.append(f"'{m.title}': {m.state}, {m.progress()} steps{extra}")
    return ToolResult.success("; ".join(parts) + ".",
                              missions=[{"title": m.title, "state": m.state, "progress": m.progress(),
                                         "milestones": [{"name": n, "done": d, "total": t} for n, d, t in m.milestones()]}
                                        for m in missions])


def mission_history(_: NoArgs, ctx: ToolContext) -> ToolResult:
    """What happened to the most recent mission: each state change with the reason and when."""
    store = MissionStore(_db(ctx))
    recent = store.recent(1)
    if not recent:
        return ToolResult.success("No missions yet.", events=[])
    mission = recent[0]
    events = store.history(mission.id)
    if not events:
        return ToolResult.success(f"'{mission.title}': {mission.state}; no recorded state changes yet.", events=[])
    lines = [f"{e['ts'][11:19]} {e['old_state'] or 'new'} -> {e['new_state']}" + (f" ({e['note']})" if e["note"] else "")
             for e in events[-12:]]
    return ToolResult.success(f"'{mission.title}' is {mission.state}. History: " + "; ".join(lines) + ".", events=events)


def _rollback_target(ctx: ToolContext):
    for mission in MissionStore(_db(ctx)).recent(5, ("completed", "paused", "failed", "cancelled")):
        if any(s.state == "done" for s in mission.steps):
            return mission
    raise ToolError("There's no recent mission with finished steps to roll back.", "no_mission")


def _rollback_question(_: NoArgs, ctx: ToolContext) -> str:
    try:
        mission = _rollback_target(ctx)
    except ToolError as exc:
        return str(exc)
    done = [s for s in mission.steps if s.state == "done"]
    can = sum(1 for s in done if s.inverse)
    return (f"Roll back '{mission.title}'? {can} of {len(done)} finished step(s) can be undone "
            f"(newest first); the rest stay as they are.")


async def rollback_mission(_: NoArgs, ctx: ToolContext) -> ToolResult:
    if ctx.executor is None:
        raise ToolError("Rolling back needs the assistant's executor.", "unavailable")
    mission = _rollback_target(ctx)
    outcome = await rollback(mission, MissionStore(_db(ctx)), ctx.executor)
    data = {"mission": mission.id, "state": outcome.mission.state}
    return ToolResult.success(outcome.message, **data) if outcome.ok else \
        ToolResult.failure("rollback_incomplete", outcome.message, **data)


class TaskCenterArgs(ToolArgs):
    show: Literal["active", "all"] = Field(default="active", description="active = running/queued/waiting only")


def task_center(args: TaskCenterArgs, ctx: ToolContext) -> ToolResult:
    from app.core import task_center as center

    tasks = center.collect(_db(ctx), include_finished=args.show == "all")
    return ToolResult.success(center.describe(tasks), tasks=[t.to_dict() for t in tasks], counts=center.counts(tasks))


TOOLS: tuple[Tool, ...] = (
    Tool("set_goal", "Save a goal the user is working towards (with deadline, priority and optional tasks), e.g. "
         "'I want to finish my project this month'. Only when the user states a goal.", set_goal, GoalArgs,
         RiskLevel.SAFE, "automation", timeout=10),
    Tool("list_goals", "The user's active goals with progress, deadlines and next tasks.", list_goals, NoArgs,
         RiskLevel.SAFE, "automation"),
    Tool("update_goal", "Change a goal: add a task, tick a task off, set progress, mark it done or drop it.",
         update_goal, UpdateGoalArgs, RiskLevel.SAFE, "automation", timeout=10,
         risk=lambda a, c: RiskLevel.MODERATE if a.action == "drop" else RiskLevel.SAFE,
         confirmation=lambda a, c: f"Drop the goal '{a.goal or 'this goal'}'?"),
    Tool("start_mission", "Plan and run a multi-step task as a mission that survives restarts: give the steps as "
         "tool calls in order. Jarvis validates and simulates the plan and asks the user to approve it before "
         "anything changes. dry_run=true only shows the plan.", start_mission, MissionArgs, RiskLevel.SAFE,
         "automation", timeout=900, risk=_mission_risk, confirmation=_mission_question),
    Tool("resume_mission", "Continue the unfinished mission (after a pause, failure or restart); skip_failed=true "
         "skips the failed step.", resume_mission, ResumeArgs, RiskLevel.SAFE, "automation", timeout=900,
         risk=_resume_risk, confirmation=_resume_question),
    Tool("cancel_mission", "Cancel the unfinished mission.", cancel_mission, NoArgs, RiskLevel.SAFE, "automation"),
    Tool("mission_status", "The recent missions, how far they got and their milestones.", mission_status, NoArgs,
         RiskLevel.SAFE, "automation"),
    Tool("mission_history", "The state changes of the most recent mission (started, paused and why, verified, "
         "completed), so you can see what happened.", mission_history, NoArgs, RiskLevel.SAFE, "automation"),
    Tool("rollback_mission", "Undo the finished steps of the most recent mission, newest first, where each can be "
         "undone; steps that can't be undone are reported, not faked. Always asks first.", rollback_mission, NoArgs,
         RiskLevel.DANGEROUS, "automation", timeout=600, confirmation=_rollback_question),
    Tool("task_center", "Background task center: missions, scheduled automations, self-improvement tests and "
         "requests still running, each with a status (queued, running, waiting for approval, paused, completed, "
         "failed, cancelled, rolled back).", task_center, TaskCenterArgs, RiskLevel.SAFE, "automation"),
)
