"""Automation tools: reminders, scheduled tasks, workflows."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import AliasChoices, Field

from app.automation.tasks import AutomationStore, AutomationTask, Step, check_name, parse_steps, validate_steps
from app.automation.triggers import (
    BatteryTrigger, DailyTrigger, IntervalTrigger, ManualTrigger, OnceTrigger, once_at, parse_days,
)
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult

DEFAULT_WORKFLOWS: dict[str, list[dict[str, Any]]] = {
    "development environment": [
        {"type": "tool", "tool": "open_in_editor", "arguments": {}},
        {"type": "tool", "tool": "open_application", "arguments": {"name": "terminal"}, "continue_on_error": True},
        {"type": "tool", "tool": "git_status", "arguments": {}, "continue_on_error": True},
        {"type": "notify", "message": "Your development environment is ready."},
    ],
}
_SEEDED_KEY = "automation.seeded"


def _store(ctx: ToolContext) -> AutomationStore:
    store = ctx.get_automation_store()
    if store is None:
        raise ToolError("Automations need the database, which isn't available.", "unavailable")
    return store


def seed_default_workflows(ctx: ToolContext) -> None:
    """Add the built-in workflows once (never re-added after the user deletes them)."""
    from app.memory.settings_store import SettingsStore

    if ctx.database is None:
        return
    settings = SettingsStore(ctx.database)
    if settings.get(_SEEDED_KEY):
        return
    store = AutomationStore(ctx.database)
    existing = {t.name.lower() for t in store.all()}
    for name, raw in DEFAULT_WORKFLOWS.items():
        if name not in existing:
            store.save(name, ManualTrigger(), parse_steps(raw), None)
    settings.set(_SEEDED_KEY, True)


def _save(ctx: ToolContext, name: str, trigger: Any, steps: list[Step]) -> tuple[AutomationTask, bool]:
    validate_steps(steps, ctx.get_tool_registry(), ctx)
    return _store(ctx).save(name, trigger, steps, trigger.next_run(ctx.clock()))


# -------------------------------------------------------------------- reminders


class ReminderArgs(ToolArgs):
    message: str = Field(min_length=1, max_length=300, validation_alias=AliasChoices("message", "text", "reminder", "what"),
                         description="What to remind the user about")
    time: str | None = Field(default=None, max_length=20, description="Clock time, e.g. '23:00' or '7:30 am'")
    in_minutes: int | None = Field(default=None, ge=1, le=86400, description="Or: minutes from now")
    date: str | None = Field(default=None, max_length=20, description="Optional date: YYYY-MM-DD, 'today' or 'tomorrow'")
    repeat: Literal["none", "daily", "weekdays", "weekends"] = Field(default="none", description="Repeat the reminder")
    days: str | None = Field(default=None, max_length=60, description="Or specific days, e.g. 'mon, wed'")


def set_reminder(args: ReminderArgs, ctx: ToolContext) -> ToolResult:
    now = ctx.clock()
    if args.repeat != "none" or args.days:
        if not args.time:
            raise ToolError("Repeating reminders need a time, e.g. '8:00'.", "missing_time")
        days = parse_days(args.days or ("daily" if args.repeat == "none" else args.repeat))
        trigger: Any = DailyTrigger(time=args.time, days=days)
    else:
        trigger = once_at(now, args.time, args.in_minutes, args.date)
    label = re.sub(r"[^\w .,'&()\-]+", " ", args.message)
    label = re.sub(r"\s+", " ", label).strip()[:40].strip() or "note"
    name = check_name(f"Reminder: {label}")
    task, _ = _save(ctx, name, trigger, [Step(type="notify", message=args.message)])
    return ToolResult.success(f"I'll remind you {trigger.describe()}: {args.message}", **task.to_dict())


class SnoozeArgs(ToolArgs):
    minutes: int = Field(default=10, ge=1, le=1440, description="Remind again in this many minutes")
    reminder: str | None = Field(default=None, max_length=80, description="Which reminder (words from it); default: "
                                                                          "the one that went off last")


def snooze_reminder(args: SnoozeArgs, ctx: ToolContext) -> ToolResult:
    """Run a one-time reminder again later. The same reminder is moved (not duplicated) and survives a restart."""
    wanted = (args.reminder or "").strip().lower()
    candidates = [t for t in _store(ctx).all()
                  if t.name.lower().startswith("reminder:") and isinstance(t.trigger, OnceTrigger)
                  and any(step.type == "notify" for step in t.steps) and (not wanted or wanted in t.name.lower())]
    if not candidates:
        raise ToolError("I don't have a one-time reminder to snooze.", "no_reminder")
    fired = [t for t in candidates if t.last_run_at]
    task = max(fired or candidates, key=lambda t: t.last_run_at or "")
    message = next(step.message for step in task.steps if step.type == "notify")
    trigger = once_at(ctx.clock(), None, args.minutes, None)
    new, _ = _save(ctx, task.name, trigger, [Step(type="notify", message=message)])
    return ToolResult.success(f"Okay, I'll remind you again in {args.minutes} minute(s): {message}", **new.to_dict())


# -------------------------------------------------------------------- schedules


class ScheduleArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=60, description="Short name, e.g. 'work apps'")
    steps: list[dict[str, Any]] | str = Field(description='Steps, e.g. [{"tool": "open_application", "arguments": '
                                                           '{"name": "chrome"}}] or [{"type": "notify", "message": "..."}]')
    time: str | None = Field(default=None, max_length=20, description="Clock time for daily runs, e.g. '8:00 am'")
    days: str | None = Field(default=None, max_length=60, description="'weekdays', 'mon, fri', ... (default every day)")
    in_minutes: int | None = Field(default=None, ge=1, le=86400, description="Run once, this many minutes from now")
    date: str | None = Field(default=None, max_length=20, description="Run once on this date (with time)")
    every_minutes: int | None = Field(default=None, ge=5, le=10080, description="Repeat every N minutes")
    battery_below: int | None = Field(default=None, ge=1, le=99, description="Run when the battery drops below this %")
    when_event: Literal["wifi", "app_opened", "usb", "file_changed"] | None = Field(
        default=None, description="Run when this happens: joining Wi-Fi, opening an app, plugging in a USB drive, "
                                  "a file added or changed in a folder")
    event_value: str | None = Field(default=None, max_length=260,
                                    description="The Wi-Fi name or app name for when_event; for file_changed the full "
                                                "folder path, optionally followed by |*.pdf")


def _trigger_for(args: ScheduleArgs, ctx: ToolContext) -> Any:
    chosen = [x for x in (args.in_minutes, args.every_minutes, args.battery_below, args.when_event) if x is not None]
    if args.when_event is not None and len(chosen) == 1:
        from app.automation.triggers import EventTrigger

        if args.when_event == "file_changed":
            from app.automation.filewatch import validate_watch

            validate_watch(args.event_value or "")
        return EventTrigger(event=args.when_event, value=args.event_value or "")
    if len(chosen) > 1:
        raise ToolError("Choose one kind of schedule: a time, in N minutes, every N minutes, or a battery level.", "ambiguous")
    if args.battery_below is not None:
        return BatteryTrigger(percent=args.battery_below)
    if args.every_minutes is not None:
        return IntervalTrigger(minutes=args.every_minutes)
    if args.in_minutes is not None or args.date:
        return once_at(ctx.clock(), args.time, args.in_minutes, args.date)
    if args.time:
        return DailyTrigger(time=args.time, days=parse_days(args.days))
    raise ToolError("When should it run? Give a time, 'in N minutes', 'every N minutes' or a battery level.", "missing_time")


def _schedule_precheck(args: ScheduleArgs, ctx: ToolContext) -> None:
    check_name(args.name)
    _trigger_for(args, ctx)
    validate_steps(parse_steps(args.steps), ctx.get_tool_registry(), ctx)


def _schedule_question(args: ScheduleArgs, ctx: ToolContext) -> str:
    trigger = _trigger_for(args, ctx)
    steps = "; ".join(s.describe() for s in parse_steps(args.steps))
    return f"Create the automation '{args.name}' to run {trigger.describe()}: {steps}?"


def schedule_task(args: ScheduleArgs, ctx: ToolContext) -> ToolResult:
    trigger = _trigger_for(args, ctx)
    task, replaced = _save(ctx, check_name(args.name), trigger, parse_steps(args.steps))
    verb = "Updated" if replaced else "Created"
    return ToolResult.success(f"{verb} '{task.name}': it runs {trigger.describe()}.", **task.to_dict())


# -------------------------------------------------------------------- workflows


class WorkflowArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=60, description="Workflow name, e.g. 'development environment'")
    steps: list[dict[str, Any]] | str = Field(description="Steps, same format as schedule_task")


def _workflow_question(args: WorkflowArgs, ctx: ToolContext) -> str:
    steps = "; ".join(s.describe() for s in parse_steps(args.steps))
    return f"Save the workflow '{args.name}': {steps}?"


def save_workflow(args: WorkflowArgs, ctx: ToolContext) -> ToolResult:
    task, replaced = _save(ctx, check_name(args.name), ManualTrigger(), parse_steps(args.steps))
    return ToolResult.success(f"{'Updated' if replaced else 'Saved'} the workflow '{task.name}' ({len(task.steps)} steps).",
                              **task.to_dict())


class NameArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=60, description="Name of the workflow or automation")


async def run_workflow(args: NameArgs, ctx: ToolContext) -> ToolResult:
    task = _store(ctx).find(args.name)
    scheduler = ctx.get_scheduler()
    report = await scheduler.run(task, manual=True)
    data = {"steps": report.steps, "failed_step": report.failed_step}
    if report.ok:
        return ToolResult.success(report.message, **data)
    return ToolResult.failure("workflow_failed", report.message, **data)


# -------------------------------------------------------------------- manage


def list_automations(_: NoArgs, ctx: ToolContext) -> ToolResult:
    tasks = _store(ctx).all()
    if not tasks:
        return ToolResult.success("There are no automations or reminders.", automations=[])
    workflows = sum(1 for t in tasks if t.is_workflow)
    return ToolResult.success(f"{len(tasks) - workflows} scheduled, {workflows} workflow(s).",
                              automations=[t.to_dict() for t in tasks])


class ManageArgs(NameArgs):
    action: Literal["pause", "resume", "delete", "run_now"] = Field(description="What to do")


def _manage_risk(args: ManageArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "delete" else RiskLevel.SAFE


async def manage_automation(args: ManageArgs, ctx: ToolContext) -> ToolResult:
    store = _store(ctx)
    task = store.find(args.name)
    if args.action == "run_now":
        return await run_workflow(NameArgs(name=task.name), ctx)
    if args.action == "delete":
        store.delete(task.id)
        return ToolResult.success(f"Deleted '{task.name}'.")
    if args.action == "pause":
        store.set_enabled(task.id, False, None)
        return ToolResult.success(f"Paused '{task.name}'.")
    store.set_enabled(task.id, True, task.trigger.next_run(ctx.clock()))
    return ToolResult.success(f"Resumed '{task.name}': it runs {task.trigger.describe()}.")


def _describe_steps(steps) -> str:
    return "; ".join(step.describe() for step in steps)


def workflow_versions(args: NameArgs, ctx: ToolContext) -> ToolResult:
    versions = _store(ctx).revisions(args.name)
    if not versions:
        return ToolResult.success(f"'{args.name}' has no earlier versions.", versions=[])
    shown = "; ".join(f"v{v['version']} ({v['reason']} {v['saved_at'][:10]}): {len(v['steps'])} step(s) - "
                      f"{_describe_steps(v['steps'])}" for v in versions[:6])
    return ToolResult.success(f"{len(versions)} earlier version(s) of '{args.name}': {shown}. Say 'restore version N "
                              f"of {args.name}' to bring one back.",
                              versions=[{"version": v["version"], "saved_at": v["saved_at"], "reason": v["reason"],
                                         "steps": len(v["steps"])} for v in versions])


class RestoreArgs(NameArgs):
    version: int = Field(ge=1, description="The version number to bring back")


def restore_workflow(args: RestoreArgs, ctx: ToolContext) -> ToolResult:
    found = next((v for v in _store(ctx).revisions(args.name) if v["version"] == args.version), None)
    if found is None:
        raise ToolError(f"'{args.name}' has no version {args.version}.", "not_found")
    task, _ = _save(ctx, check_name(args.name), found["trigger"], found["steps"])  # checked again, like new
    return ToolResult.success(f"Restored version {args.version} of '{task.name}' ({len(task.steps)} steps).",
                              **task.to_dict())


TOOLS: tuple[Tool, ...] = (
    Tool("set_reminder", "Remind the user of something at a time, in N minutes, or repeatedly (daily/weekdays).",
         set_reminder, ReminderArgs, RiskLevel.SAFE, "automation"),
    Tool("snooze_reminder", "Snooze the reminder that just went off (or a named one): remind again in N minutes.",
         snooze_reminder, SnoozeArgs, RiskLevel.SAFE, "automation"),
    Tool("schedule_task", "Create an automation that runs tools on a schedule (e.g. every weekday at 8:00 open apps), "
         "every N minutes, or when the battery is low. Asks the user first.", schedule_task, ScheduleArgs,
         RiskLevel.MODERATE, "automation", precheck=_schedule_precheck, confirmation=_schedule_question),
    Tool("save_workflow", "Save a named multi-step workflow the user can run later. Asks the user first.",
         save_workflow, WorkflowArgs, RiskLevel.MODERATE, "automation",
         precheck=lambda a, c: (check_name(a.name), validate_steps(parse_steps(a.steps), c.get_tool_registry(), c)) and None,
         confirmation=_workflow_question),
    Tool("run_workflow", "Run a saved workflow, e.g. 'development environment'.", run_workflow, NameArgs,
         RiskLevel.SAFE, "automation", timeout=900),
    Tool("list_automations", "List reminders, scheduled automations and workflows.", list_automations, NoArgs,
         RiskLevel.SAFE, "automation"),
    Tool("manage_automation", "Pause, resume, delete or run now an automation or reminder.", manage_automation,
         ManageArgs, RiskLevel.SAFE, "automation", timeout=900, risk=_manage_risk,
         confirmation=lambda a, c: f"Delete the automation '{a.name}'?"),
    Tool("workflow_versions", "Earlier versions of a workflow or automation (kept when it was changed or deleted).",
         workflow_versions, NameArgs, RiskLevel.SAFE, "automation"),
    Tool("restore_workflow", "Bring back an earlier version of a workflow or automation (the current one is kept as "
         "a version too).", restore_workflow, RestoreArgs, RiskLevel.MODERATE, "automation",
         confirmation=lambda a, c: f"Restore version {a.version} of '{a.name}'?"),
)
