"""Automation tools: reminders, scheduled tasks, workflows."""

from __future__ import annotations

import json
import re
from pathlib import Path
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


def _save(ctx: ToolContext, name: str, trigger: Any, steps: list[Step],
          parameters: dict[str, dict[str, Any]] | None = None) -> tuple[AutomationTask, bool]:
    from app.automation.tasks import bind, check_parameters, clean_parameters

    parameters = clean_parameters(parameters) if parameters is not None else None
    if parameters:
        check_parameters(steps, parameters)
    # the steps are validated with their defaults filled in, exactly as they would run
    validate_steps(bind(steps, parameters, {}) if parameters else steps, ctx.get_tool_registry(), ctx)
    return _store(ctx).save(name, trigger, steps, trigger.next_run(ctx.clock()), parameters)


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
    when_event: Literal["wifi", "app_opened", "usb", "file_changed", "phone_charging", "phone_battery_low"] | None = Field(
        default=None, description="Run when this happens: joining Wi-Fi, opening an app, plugging in a USB drive, "
                                  "a file added or changed in a folder, your phone being plugged in / unplugged, "
                                  "your phone's battery dropping low")
    event_value: str | None = Field(default=None, max_length=260,
                                    description="The Wi-Fi name or app name for when_event; for file_changed the full "
                                                "folder path, optionally followed by |*.pdf; for phone_charging "
                                                "'started' or 'stopped'; for phone_battery_low a percent (default 20)")


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


class TeachArgs(ToolArgs):
    action: Literal["start", "stop", "cancel", "status"] = Field(
        description="start = begin a teach session (name needed); stop = save what Jarvis did since as a workflow; "
                    "cancel = forget it; status = what has been noted so far")
    name: str = Field(default="", max_length=60, description="Workflow name (start)")


def _teach_risk(args: TeachArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "stop" else RiskLevel.SAFE


def _teach_question(args: TeachArgs, _ctx: ToolContext) -> str:
    from app.automation.teach import TEACHER

    session = TEACHER.session
    if session is None:
        return "Save the taught workflow?"
    steps = "; ".join(Step.model_validate({"tool": s["tool"], "arguments": s["arguments"]}).describe() for s in session.steps)
    return f"Save the workflow '{session.name}' from what I did: {steps or 'nothing yet'}?"


def teach_workflow(args: TeachArgs, ctx: ToolContext) -> ToolResult:
    from app.automation.teach import TEACHER

    if args.action == "start":
        name = check_name(args.name)
        TEACHER.start(name)
        return ToolResult.success(f"Teaching '{name}'. Now tell me what to do, one thing at a time - I note only what I "
                                  "actually do (never your keyboard or mouse). Say 'done teaching' to save it.", teaching=name)
    session = TEACHER.session
    if session is None:
        return ToolResult.success("No teach session is running. Say 'teach you a routine called ...' to start one.")
    if args.action == "status":
        done = "; ".join(s["tool"] for s in session.steps) or "nothing yet"
        return ToolResult.success(f"Teaching '{session.name}': {len(session.steps)} step(s) so far - {done}.",
                                  steps=len(session.steps), skipped=session.skipped)
    if args.action == "cancel":
        TEACHER.cancel()
        return ToolResult.success(f"Stopped teaching '{session.name}'. Nothing was saved.")
    if not session.steps:
        raise ToolError("I haven't done anything yet this session, so there is nothing to save. Tell me what to do first "
                        "(or say 'cancel teaching').", "nothing_recorded")
    task, replaced = _save(ctx, session.name, ManualTrigger(), parse_steps(session.steps))
    TEACHER.cancel()
    note = f" (left out: {'; '.join(session.skipped)})" if session.skipped else ""
    return ToolResult.success(f"{'Updated' if replaced else 'Saved'} the workflow '{task.name}' with {len(task.steps)} "
                              f"step(s). Run it any time: 'run {task.name}'.{note}", **task.to_dict())


class NameArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=60, description="Name of the workflow or automation")


class RunWorkflowArgs(NameArgs):
    resume: bool = Field(default=False, description="Continue a workflow that stopped, from the step it stopped at")
    params: dict[str, str] = Field(default_factory=dict,
                                   description="Values for the workflow's named inputs, e.g. {\"site\": \"github.com\"}. An input "
                                               "left out uses its saved default")
    on_uncertain: Literal["skip", "repeat"] | None = Field(
        default=None, description="Only with resume: when the stopped step may or may not have happened and it changes "
                                  "something, say 'skip' (it did happen) or 'repeat' (it did not). Without this, such a "
                                  "step is never repeated automatically")


async def run_workflow(args: RunWorkflowArgs, ctx: ToolContext) -> ToolResult:
    task = _store(ctx).find(args.name)
    scheduler = ctx.get_scheduler()
    report = await scheduler.run(task, manual=True, resume=args.resume, uncertain=args.on_uncertain if args.resume else None,
                                 params=args.params or None)
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
    from app.automation import reliability

    rows = []
    for t in tasks:
        row = t.to_dict()
        try:
            stat = reliability.stats(ctx.database, t.name)
            if stat.runs:
                row["reliability"] = stat.describe()
        except Exception:
            pass
        rows.append(row)
    return ToolResult.success(f"{len(tasks) - workflows} scheduled, {workflows} workflow(s).", automations=rows)


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


class ExportArgs(NameArgs):
    description: str = Field(default="", max_length=300, description="What the workflow is for")
    expected_outcome: str = Field(default="", max_length=300, description="What should be true when it finishes")


def export_workflow(args: ExportArgs, ctx: ToolContext) -> ToolResult:
    """Write a workflow to a JSON file in the exports folder, with the tools and risk it needs declared."""
    from app.automation import portable

    task = _store(ctx).find(args.name)
    doc = portable.export_document(task, ctx.get_tool_registry(), ctx, args.description, args.expected_outcome)
    base = ctx.paths.data_dir if ctx.paths is not None else Path.home() / ".jarvis"
    folder = base / "exports" / "workflows"
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", task.name).strip("_") or "workflow"
    path = folder / f"{safe}.jarvis-workflow.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return ToolResult.success(f"Exported '{task.name}' to {path.name} (needs: {', '.join(doc['requires']['tools']) or 'nothing'}; "
                              f"highest risk {doc['requires']['max_risk']}). The file is data only; its schedule is not exported.",
                              path=str(path), requires=doc["requires"])


class ImportArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=400, description="Path of a .jarvis-workflow.json file")
    replace: bool = Field(default=False, description="Replace a workflow with the same name (the old one is kept as a version)")
    check_only: bool = Field(default=False, description="Only check the file and say what importing would do")


def _import_read(args: ImportArgs, ctx: ToolContext) -> str:
    path = ctx.get_path_policy().resolve(args.path, must_exist=True)
    if not path.is_file():
        raise ToolError("That is not a file.", "not_a_file")
    if path.stat().st_size > 64_000:
        raise ToolError("That file is too large to be a workflow.", "too_large")
    return path.read_text(encoding="utf-8", errors="replace")


def _import_precheck(args: ImportArgs, ctx: ToolContext) -> None:
    from app.automation import portable

    report = portable.validate_import(_import_read(args, ctx), ctx.get_tool_registry(), ctx)
    if not report.ok:
        raise ToolError(report.text(), "invalid_workflow")


def _import_question(args: ImportArgs, ctx: ToolContext) -> str:
    from app.automation import portable

    report = portable.validate_import(_import_read(args, ctx), ctx.get_tool_registry(), ctx)
    steps = "; ".join(s.describe() for s in report.steps)
    return f"Import the workflow '{report.name}' (highest risk {report.max_risk})? Steps: {steps}. It will not run until you ask."


def import_workflow(args: ImportArgs, ctx: ToolContext) -> ToolResult:
    from app.automation import portable

    report = portable.validate_import(_import_read(args, ctx), ctx.get_tool_registry(), ctx)
    if not report.ok:
        raise ToolError(report.text(), "invalid_workflow")
    store = _store(ctx)
    exists = any(t.name.lower() == report.name.lower() for t in store.all())
    if args.check_only:
        return ToolResult.success(report.text() + (" A workflow with this name exists." if exists else ""),
                                  name=report.name, exists=exists, warnings=report.warnings)
    if exists and not args.replace:
        raise ToolError(f"A workflow called '{report.name}' already exists. Say 'replace' to overwrite it "
                        "(the old one is kept as a version).", "exists")
    task, replaced = _save(ctx, report.name, ManualTrigger(), report.steps)
    return ToolResult.success(f"{'Replaced' if replaced else 'Imported'} the workflow '{task.name}' ({len(task.steps)} steps). "
                              "It has not been run; its schedule is never imported.", name=task.name)


class RestoreArgs(NameArgs):
    version: int = Field(ge=1, description="The version number to bring back")


def restore_workflow(args: RestoreArgs, ctx: ToolContext) -> ToolResult:
    found = next((v for v in _store(ctx).revisions(args.name) if v["version"] == args.version), None)
    if found is None:
        raise ToolError(f"'{args.name}' has no version {args.version}.", "not_found")
    task, _ = _save(ctx, check_name(args.name), found["trigger"], found["steps"])  # checked again, like new
    return ToolResult.success(f"Restored version {args.version} of '{task.name}' ({len(task.steps)} steps).",
                              **task.to_dict())


class SimulateArgs(ToolArgs):
    workflow: str = Field(min_length=1, max_length=80, description="Name of a saved workflow or automation to simulate")
    what_if: Literal["normal", "tool_fails", "tool_times_out", "permission_denied", "response_lost", "crash_and_restart",
                     "duplicate_trigger", "emergency_stop"] = Field(
        default="normal", description="The situation to play through (nothing real runs in any of them)")
    tool: str | None = Field(default=None, max_length=60, description="Which step's tool the situation happens to "
                                                                       "(default: the first tool step)")
    resumes: int = Field(default=1, ge=0, le=3, description="How many times 'resume' is said after a failure")
    treat_as_idempotent: bool = Field(default=False, description="Pretend repeating that tool's effect is harmless")
    on_uncertain: Literal["skip", "repeat"] | None = Field(
        default=None, description="What you would tell 'resume' about a state-changing step whose outcome is unknown")


def simulate_workflow_tool(args: SimulateArgs, ctx: ToolContext) -> ToolResult:
    """Play a saved workflow through a what-if with scripted pretend tools, on a scratch database. Changes nothing."""
    from app.automation import simulator as sim

    if ctx.database is None:
        raise ToolError("Workflows need the database.", "no_database")
    task = AutomationStore(ctx.database).find(args.workflow)
    steps = [s.model_dump(exclude_none=True) for s in task.steps]
    tool_steps = [s["tool"] for s in steps if s.get("type", "tool") == "tool"]
    if not tool_steps:
        raise ToolError(f"'{task.name}' has no tool steps to simulate.", "nothing_to_simulate")
    target = args.tool or tool_steps[0]
    if target not in tool_steps:
        raise ToolError(f"'{task.name}' has no step using '{target}'. Its tools: {', '.join(dict.fromkeys(tool_steps))}.",
                        "unknown_step")
    scenario = sim.scenario_for(args.what_if, target, args.resumes, args.treat_as_idempotent, args.on_uncertain)
    from app.tools.catalog import build_default_registry

    report = sim.simulate_workflow(steps, scenario, build_default_registry())
    return ToolResult.success(report.describe(), simulated=True, final_ok=report.final_ok, duplicated=report.duplicated,
                              effects=report.effects)


class BackgroundArgs(ToolArgs):
    action: Literal["status", "pause", "resume", "stop"] = Field(
        description="status = is a background host running and what is it doing; pause / resume / stop it")


def _background_risk(args: BackgroundArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.SAFE if args.action == "status" else RiskLevel.MODERATE


def background_mode(args: BackgroundArgs, ctx: ToolContext) -> ToolResult:
    """See and control the optional windowless background host (python run.py --background). Starting it is never done
    from here: only you start it, from a terminal or the opt-in scheduled task."""
    from app.automation import background

    if ctx.paths is None:
        raise ToolError("Background mode needs the data folder.", "no_context")
    state = background.read_state(ctx.paths.data_dir)
    running = background.is_running(ctx.paths.data_dir)
    if args.action == "status":
        if not running:
            return ToolResult.success("No background host is running. Automations run only while this window is open "
                                      "(or if you start `python run.py --background`).", running=False)
        text = (f"The background host is {state['status']} (pid {state['pid']}, started {state['started_at'][:16]}, last "
                f"heartbeat {state['heartbeat'][11:19]}); {len(state.get('running_now', []))} automation(s) running now.")
        return ToolResult.success(text, running=True, **{k: state.get(k) for k in ("status", "notices", "stalled")})
    if not running:
        return ToolResult.success(f"Nothing to {args.action}: no background host is running.", running=False)
    background.request(ctx.paths.data_dir, args.action)
    return ToolResult.success(f"Asked the background host to {args.action}; it reads the request within a few seconds.",
                              running=True, requested=args.action)


class MobileSyncArgs(ToolArgs):
    action: Literal["status", "enable", "disable", "forget"] = Field(
        description="status = what each paired phone may sync; enable / disable one dataset for a phone; forget = delete "
                    "a phone's sync bookkeeping")
    phone: str = Field(default="", max_length=80, description="The phone's name (as shown in Jarvis Mobile settings); "
                                                              "optional when only one phone is paired")
    dataset: Literal["todos", "notifications", ""] = Field(default="", description="todos or notifications")


def _mobile_sync_risk(args: MobileSyncArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.SAFE if args.action == "status" else RiskLevel.MODERATE


def mobile_sync(args: MobileSyncArgs, ctx: ToolContext) -> ToolResult:
    """Choose, on the PC, which data a paired phone may sync. Nothing is shared until you enable it here."""
    from app.remote.mobile import MobileRegistry
    from app.remote.sync import DATASETS, SyncStore

    if ctx.database is None:
        raise ToolError("Phone sync needs the database.", "no_context")
    phones = [d for d in MobileRegistry(ctx.database).all() if d.trust in ("trusted", "restricted")]
    if not phones:
        return ToolResult.success("No paired phone is trusted yet, so nothing is synced.", phones=0)
    if args.phone:
        phones = [d for d in phones if d.name.lower() == args.phone.lower()]
        if not phones:
            raise ToolError(f"No trusted phone is named '{args.phone}'.", "not_found")
    store = SyncStore(ctx.database)
    if args.action == "status":
        lines = []
        for device in phones:
            on = store.enabled(device.id)
            lines.append(f"{device.name}: syncing {', '.join(on) if on else 'nothing'}"
                         + ("" if "sync" in device.permissions else " (the phone lacks the 'sync' permission)"))
        return ToolResult.success("\n".join(lines) + "\nSyncing is by polling, not push; the phone must ask.", phones=len(phones))
    if len(phones) > 1:
        raise ToolError("More than one phone is paired; say which one (phone=<name>).", "ambiguous")
    device = phones[0]
    if args.action == "forget":
        removed = store.forget_device(device.id)
        return ToolResult.success(f"Deleted {device.name}'s sync settings and history ({removed} rows). Your to-dos are unchanged.")
    if args.dataset not in DATASETS:
        raise ToolError("Say which data: todos or notifications.", "invalid")
    store.set_enabled(device.id, args.dataset, args.action == "enable")
    note = "" if "sync" in device.permissions or args.action == "disable" else (
        " The phone still needs the 'sync' permission (Jarvis Mobile settings in the window).")
    return ToolResult.success(f"{'Enabled' if args.action == 'enable' else 'Disabled'} syncing {args.dataset} with {device.name}.{note}")


TOOLS: tuple[Tool, ...] = (
    Tool("mobile_sync", "Choose which data a paired Android phone may sync with this PC (to-dos, generic notifications) "
         "or turn it off or delete the phone's sync data. Nothing syncs until you enable it.", mobile_sync, MobileSyncArgs,
         RiskLevel.SAFE, "phone", timeout=15, risk=_mobile_sync_risk,
         confirmation=lambda a, c: f"{a.action.capitalize()} {a.dataset or 'phone'} sync?"),
    Tool("simulate_workflow", "Play a saved workflow through a what-if (a step fails, times out, is denied, loses its "
         "answer, the program crashes and restarts, it is triggered twice, or the emergency stop is pressed) using pretend "
         "tools on a scratch database. Nothing real runs; every result is labelled SIMULATED. Finds design problems such "
         "as a step that would run twice after a crash.", simulate_workflow_tool, SimulateArgs, RiskLevel.SAFE,
         "automation", timeout=60),
    Tool("background_mode", "See or control the optional windowless background host that runs scheduled automations: "
         "status, pause, resume or stop. It never starts it. Steps that need your approval are refused there.",
         background_mode, BackgroundArgs, RiskLevel.SAFE, "automation", timeout=15, risk=_background_risk,
         confirmation=lambda a, c: f"{a.action.capitalize()} the background host?"),
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
    Tool("teach_workflow", "Teach Jarvis a routine by showing: start a session, do the routine by telling Jarvis what "
         "to do, then stop to save what it actually did as a workflow (asks first). Never records the keyboard, mouse "
         "or screen.", teach_workflow, TeachArgs, RiskLevel.SAFE, "automation", risk=_teach_risk,
         confirmation=_teach_question),
    Tool("run_workflow", "Run a saved workflow, e.g. 'development environment'. resume=true continues one that "
         "stopped, from the step it stopped at.", run_workflow, RunWorkflowArgs,
         RiskLevel.SAFE, "automation", timeout=900),
    Tool("export_workflow", "Export a saved workflow to a portable JSON file (data only) that declares the tools and "
         "risk it needs.", export_workflow, ExportArgs, RiskLevel.MODERATE, "automation",
         confirmation=lambda a, c: f"Write the workflow '{a.name}' to a file in Jarvis's exports folder?"),
    Tool("import_workflow", "Import a workflow file (.jarvis-workflow.json): checked strictly, saved as a manual "
         "workflow, never run by importing, never more powerful than it declares. check_only=true just reports.",
         import_workflow, ImportArgs, RiskLevel.MODERATE, "automation", precheck=_import_precheck,
         confirmation=_import_question),
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
