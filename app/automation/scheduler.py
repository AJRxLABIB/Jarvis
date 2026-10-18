"""Runs automations while the assistant is open.

* Checks every 30 s (cheap: one small SQLite query), so it costs ~nothing when idle.
* Every tool step goes through the normal ToolExecutor: validation, permissions,
  confirmation (a dialog nobody answers is cancelled after 60 s), rate limit, audit.
* A failed step stops the run (unless the step says continue_on_error).
* Missed while the app was closed: one-off tasks up to 1 hour late still run;
  older ones are marked missed. Repeating tasks just continue from now.
"""

from __future__ import annotations

from app.utils.timefmt import time12

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psutil

from app.agent.tool_manager import ToolExecution, ToolExecutor
from app.ai.base import ToolCall
from app.automation.tasks import AutomationStore, AutomationTask
from app.automation.triggers import BatteryTrigger, EventTrigger, OnceTrigger
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("automation")
CHECK_SECONDS = 30.0
LATE_GRACE = timedelta(hours=1)
MAX_CONCURRENT_RUNS = 4          # backpressure: more than this many automations at once are refused, not queued forever
STALLED_AFTER = timedelta(minutes=30)

NotifyCallback = Callable[[str, str], None]  # (title, message)

# failure codes that mean "the tool did nothing" (refused before it ran). Every other failure - a timeout, an error inside
# the tool, a result that could not be verified - leaves it unknown whether the step took effect.
DEFINITE_FAILURES = frozenset({"unknown_tool", "invalid_arguments", "offline", "permission_error", "agent_not_allowed",
                               "blocked", "rate_limited", "declined"})


@dataclass
class RunReport:
    task: AutomationTask
    ok: bool
    steps: list[str] = field(default_factory=list)
    failed_step: int | None = None
    message: str = ""


def _battery() -> tuple[float, bool] | None:
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    return (battery.percent, bool(battery.power_plugged)) if battery else None


def _pc_events() -> dict:
    """What the event triggers look at: the Wi-Fi name, running apps, removable drives."""
    snapshot: dict = {"wifi": "", "apps": set(), "usb": set()}
    try:
        from app.tools import winapi

        if winapi.is_windows():
            snapshot["wifi"] = winapi.wifi_name()
    except Exception:
        pass
    try:
        snapshot["apps"] = {(p.info.get("name") or "").lower().removesuffix(".exe")
                            for p in psutil.process_iter(["name"])}
    except Exception:
        pass
    try:
        snapshot["usb"] = {d.device for d in psutil.disk_partitions(all=False) if "removable" in (d.opts or "")}
    except Exception:
        pass
    return snapshot


MAX_EVENT_RUNS_PER_HOUR = 12   # an event-started automation firing more often than this is held back (loop guard)

class Scheduler:
    def __init__(
        self,
        store: AutomationStore,
        executor: ToolExecutor,
        notify: NotifyCallback,
        *,
        clock: Callable[[], datetime] | None = None,
        battery: Callable[[], tuple[float, bool] | None] = _battery,
        on_run: Callable[[RunReport], None] | None = None,
        events: Callable[[], dict] = _pc_events,
    ) -> None:
        self.store = store
        self.executor = executor
        self.notify = notify
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.battery = battery
        self.on_run = on_run
        self._battery_fired: set[str] = set()  # re-armed when plugged in or back above the level
        self.events = events
        self._last_events: dict | None = None
        self._watch: dict[str, Any] = {}  # task id -> last folder listing (baseline on the first look)
        self._fired: dict[str, deque] = {}  # task id -> when its event trigger last started it (rate cap)
        self._capped: set[str] = set()
        self._task: asyncio.Task | None = None
        self._running: set[str] = set()
        self._started: dict[str, datetime] = {}
        self.last_tick_at: datetime | None = None
        self.refused_runs = 0
        self.housekeeping: list[Callable[[], Awaitable[Any]]] = []  # quiet chores (health history, backups)

    # --------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        await self.catch_up()
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("scheduler tick failed")
            for chore in list(self.housekeeping):
                try:
                    await chore()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.warning("a housekeeping chore failed", exc_info=True)
            await asyncio.sleep(CHECK_SECONDS)

    def health(self) -> dict[str, Any]:
        """Is the worker alive and keeping up? Running automations with their age, stalled ones, the last loop time."""
        now = self.clock()
        running = [{"task": tid, "seconds": int((now - since).total_seconds())} for tid, since in self._started.items()
                   if tid in self._running]
        stalled = [r for r in running if r["seconds"] > STALLED_AFTER.total_seconds()]
        lag = None if self.last_tick_at is None else int((now - self.last_tick_at).total_seconds())
        return {"loop_alive": self._task is not None and not self._task.done(), "running": running, "stalled": stalled,
                "seconds_since_last_check": lag, "refused_runs": self.refused_runs, "limit": MAX_CONCURRENT_RUNS}

    # ------------------------------------------------------------------- timing

    def next_run(self, task: AutomationTask, after: datetime | None = None) -> datetime | None:
        return task.trigger.next_run(after or self.clock())

    async def catch_up(self, why: str = "while the assistant was closed", *, after_sleep: bool = False) -> None:
        """Deal with runs that were due while the assistant was closed (or the PC was asleep): one-time ones up to an hour
        late still run; older ones and missed repeats are TOLD to you, never silently dropped or fired in a burst."""
        now = self.clock()
        for task in self.store.all():
            if task.last_status == "running" and task.id not in self._running:
                # claimed by a run that never finished (crash, power loss): say so, never silently repeat it
                self.store.record_run(task.id, "interrupted (the assistant stopped while it was running)", None,
                                      disable=isinstance(task.trigger, OnceTrigger))
                self.notify("Interrupted", f"'{task.name}' was running when the assistant stopped. "
                                           "It was not run again; check what it did before running it again.")
                continue
            if not task.enabled or not task.next_run_at:
                continue
            due = datetime.fromisoformat(task.next_run_at)
            if due > now:
                continue
            if isinstance(task.trigger, OnceTrigger):
                if now - due > LATE_GRACE:
                    self.store.record_run(task.id, "missed (the assistant was not running)", None, disable=True)
                    self.notify("Missed", f"'{task.name}' was due at {time12(due)} {why}.")
                # else: tick() runs it now, a little late
            elif after_sleep and now - due <= LATE_GRACE:
                continue                          # just woke up and it's only a little late: tick() runs it now
            else:
                following = self.next_run(task, now)
                self.store.record_run(task.id, f"skipped {why}", following)
                upcoming = f" Next: {time12(following)}." if following else ""
                self.notify("Missed", f"'{task.name}' was due at {time12(due)} {why}; it was skipped, not run late."
                                      + upcoming)

    async def tick(self) -> list[RunReport]:
        from app.core import emergency

        if emergency.STATE.flags.get("automation_disabled"):
            return []  # emergency controls: automations are off until you turn them on in the window
        now = self.clock()
        previous, self.last_tick_at = self.last_tick_at, now
        if previous is not None and now - previous > timedelta(seconds=CHECK_SECONDS * 6):
            # the loop didn't run for minutes: the PC was asleep (or Jarvis was frozen). Treat what fell due meanwhile
            # like a restart - nothing fires in a sudden burst, and missed ones are reported.
            log_event(_log, "scheduler_resumed_after_gap", seconds=int((now - previous).total_seconds()))
            await self.catch_up("while the PC was asleep", after_sleep=True)
        reports = []
        tasks = self.store.all()
        happened = self._events_now() if any(isinstance(t.trigger, EventTrigger) and t.trigger.event != "file_changed" and t.enabled
                                             for t in tasks) else None
        for task in tasks:
            if not task.enabled or task.id in self._running:
                continue
            if isinstance(task.trigger, BatteryTrigger):
                if self._battery_due(task) and self._event_gate(task):
                    reports.append(await self.run(task))
                continue
            if isinstance(task.trigger, EventTrigger) and task.trigger.event == "file_changed":
                if self._files_due(task) and self._event_gate(task):
                    reports.append(await self.run(task))
                    self._rebaseline(task)       # what the automation itself changed in the folder doesn't re-fire it
                continue
            if isinstance(task.trigger, EventTrigger):
                if happened is not None and self._event_due(task.trigger, *happened) and self._event_gate(task):
                    reports.append(await self.run(task))
                continue
            if task.next_run_at and datetime.fromisoformat(task.next_run_at) <= now:
                reports.append(await self.run(task))
        return reports

    def _events_now(self) -> tuple[dict, dict] | None:
        """(before, now) snapshots; None the first time (nothing "happened" yet)."""
        try:
            current = self.events()
        except Exception:
            return None
        before, self._last_events = self._last_events, current
        return (before, current) if before is not None else None

    @staticmethod
    def _event_due(trigger: EventTrigger, before: dict, now: dict) -> bool:
        wanted = trigger.value.strip().lower()
        if trigger.event == "wifi":
            joined = (now.get("wifi") or "").lower()
            return bool(joined) and joined != (before.get("wifi") or "").lower() and (not wanted or joined == wanted)
        if trigger.event == "app_opened":
            new = set(now.get("apps", set())) - set(before.get("apps", set()))
            wanted = wanted.removesuffix(".exe")
            return any(wanted in app for app in new) if wanted else False
        return bool(set(now.get("usb", set())) - set(before.get("usb", set())))

    def _files_due(self, task: AutomationTask) -> bool:
        from app.automation import filewatch

        now = filewatch.snapshot(task.trigger.value)
        before = self._watch.get(task.id)
        if now is not None:
            self._watch[task.id] = now  # an unreadable folder keeps the old baseline
        return bool(filewatch.changed(before, now))

    def _rebaseline(self, task: AutomationTask) -> None:
        from app.automation import filewatch

        now = filewatch.snapshot(task.trigger.value)
        if now is not None:
            self._watch[task.id] = now

    def _event_gate(self, task: AutomationTask) -> bool:
        """Event-started automations (files, battery, Wi-Fi, apps, USB): at most MAX_EVENT_RUNS_PER_HOUR each (a
        trigger that keeps firing - e.g. a folder something writes to all the time - is held back and reported once),
        and none during quiet hours when AUTOMATION_QUIET_HOURS is on. Time-scheduled ones run as you scheduled them."""
        now = self.clock()
        settings = getattr(getattr(self.executor, "context", None), "settings", None)
        if settings is not None and getattr(settings, "automation_quiet_hours", False):
            from app.core.proactive import in_quiet_hours, parse_quiet_hours

            if in_quiet_hours(now, parse_quiet_hours(getattr(settings, "quiet_hours", ""))):
                log_event(_log, "automation_quiet_hours", name=task.name)
                return False
        fired = self._fired.setdefault(task.id, deque())
        while fired and (now - fired[0]).total_seconds() > 3600:
            fired.popleft()
        if len(fired) >= MAX_EVENT_RUNS_PER_HOUR:
            if task.id not in self._capped:
                self._capped.add(task.id)
                log_event(_log, "automation_rate_capped", name=task.name, per_hour=MAX_EVENT_RUNS_PER_HOUR)
                try:
                    self.notify(f"Automation held back: {task.name}",
                                f"Its trigger fired {MAX_EVENT_RUNS_PER_HOUR} times within an hour, so I stopped "
                                "running it for now (it may be triggering itself). It runs again later; check its trigger.")
                except Exception:
                    pass
            return False
        self._capped.discard(task.id)
        fired.append(now)
        return True

    def _step_allowed(self, step: Any, previous_ok: bool | None) -> bool:
        """The step's optional condition ('when'). Unknown battery state counts as 'not on battery'."""
        when = getattr(step, "when", None)
        if when is None:
            return True
        if when == "previous_ok":
            return previous_ok is not False
        if when == "previous_failed":
            return previous_ok is False
        if when in ("weekday", "weekend"):
            return (self.clock().weekday() < 5) == (when == "weekday")
        try:
            reading = self.battery()
        except Exception:
            reading = None
        plugged = True if reading is None else reading[1]
        return (not plugged) if when == "on_battery" else plugged

    def _battery_due(self, task: AutomationTask) -> bool:
        reading = self.battery()
        if reading is None:
            return False
        percent, plugged = reading
        if plugged or percent >= task.trigger.percent + 2:
            self._battery_fired.discard(task.id)  # re-arm
            return False
        if percent < task.trigger.percent and task.id not in self._battery_fired:
            self._battery_fired.add(task.id)
            return True
        return False

    # -------------------------------------------------------------------- run

    def _repeat_needs_your_say(self, step: Any, outcome: str | None) -> bool:
        """Would running this checkpointed step again risk doing it twice?

        True only for a step that changes something (not read-only) whose outcome is UNKNOWN - the program stopped during
        it, or it timed out, or its answer was lost. A step that failed cleanly did nothing and is simply retried.
        """
        if outcome == "failed" or getattr(step, "type", "tool") != "tool":
            return False
        registry = getattr(self.executor, "registry", None)
        tool = registry.get(step.tool or "") if registry is not None else None
        if registry is None:
            return False                      # no tool list to judge by (a bare test executor): old behaviour
        if tool is None:
            return True                       # unknown tool: be careful
        return tool.risk is not None or int(tool.level) > 0

    async def run(self, task: AutomationTask, *, manual: bool = False, resume: bool = False,
                  uncertain: str | None = None) -> RunReport:
        """``uncertain``: what to do when ``resume`` meets a step whose outcome is unknown - 'repeat' or 'skip'."""
        from app.core.runs import Run, RunStore, running

        if task.id in self._running:  # a manual run must not overlap a scheduled one (or itself)
            log_event(_log, "automation_overlap_refused", name=task.name, manual=manual)
            return RunReport(task, ok=False, message=f"'{task.name}' is already running; not started a second time.")
        if len(self._running) >= MAX_CONCURRENT_RUNS:  # backpressure: say so instead of piling work up
            self.refused_runs += 1
            log_event(_log, "automation_backpressure", name=task.name, running=len(self._running))
            return RunReport(task, ok=False, message=f"Too many automations are running at once "
                                                     f"({len(self._running)}); '{task.name}' was not started.")
        self._running.add(task.id)  # claimed here, with no await in between, so two callers can't both pass
        self._started[task.id] = self.clock()
        database = getattr(getattr(self.executor, "context", None), "database", None)
        with running(Run("automation", task.name), RunStore(database) if database is not None else None):
            return await self._run(task, manual=manual, resume=resume, uncertain=uncertain)

    async def _run(self, task: AutomationTask, *, manual: bool = False, resume: bool = False,
                   uncertain: str | None = None) -> RunReport:
        self._running.add(task.id)
        report = RunReport(task, ok=True)
        try:
            if isinstance(task.trigger, OnceTrigger) and not task.is_workflow and not manual:
                # at-most-once: claim a one-time task BEFORE its steps run, so a crash mid-run can never repeat them
                self.store.record_run(task.id, "running", None, disable=True)
            log_event(_log, "automation_started", name=task.name, manual=manual)
            previous_ok: bool | None = None
            from app.core import emergency

            first = 1
            if task.is_workflow:  # checkpoints: a workflow that stopped can continue from the step it stopped at
                saved = self.store.progress(task.id) if resume else None
                if saved is not None and 1 <= saved <= len(task.steps):
                    first = saved
                    step_here = task.steps[saved - 1]
                    if self._repeat_needs_your_say(step_here, self.store.progress_outcome(task.id)):
                        if uncertain == "skip":
                            first = saved + 1
                            report.steps.append(f"step {saved} ({step_here.tool}) skipped on your say-so: you said it already happened")
                        elif uncertain == "repeat":
                            report.steps.append(f"repeating step {saved} ({step_here.tool}) on your say-so: you said it did not happen")
                        else:
                            report.ok, report.failed_step = False, saved
                            report.message = (f"'{task.name}' was NOT resumed: step {saved} ({step_here.tool}) was in progress when "
                                              "it stopped, so it may already have happened and repeating it could do it twice. "
                                              "Check, then resume with on_uncertain='skip' (it did happen) or "
                                              "on_uncertain='repeat' (it did not).")
                            self.notify(task.name, report.message)
                            return report
                    if first == saved:
                        report.steps.append(f"resumed at step {saved} (steps 1-{saved - 1} were already done)")
                    else:
                        report.steps.append(f"resumed after step {saved} (steps 1-{saved} are treated as done)")
                else:
                    self.store.clear_progress(task.id)
            for number, step in enumerate(task.steps, start=1):
                if number < first:
                    continue
                if task.is_workflow:
                    self.store.set_progress(task.id, number)  # written BEFORE the step: a crash resumes AT it
                if not manual and emergency.STATE.flags.get("automation_disabled"):
                    report.ok, report.failed_step = False, number  # the emergency stop reaches a run already in progress
                    report.message = f"'{task.name}' was stopped before step {number} by the emergency controls."
                    self.notify(task.name, report.message)
                    break
                if not self._step_allowed(step, previous_ok):
                    report.steps.append(f"skipped (only if {step.when.replace('_', ' ')})")
                    continue
                if step.type == "notify":
                    self.notify(task.name, step.message or "")
                    report.steps.append(f"said: {step.message}")
                    previous_ok = True
                    continue
                execution: ToolExecution = await self.executor.execute(ToolCall(step.tool or "", dict(step.arguments)))
                result = execution.result
                report.steps.append(f"{step.tool}: {'ok' if result.ok else 'failed'} - {result.message}")
                previous_ok = result.ok
                if not result.ok and task.is_workflow:
                    # a clean refusal did nothing; anything else (a timeout, a crash inside the tool, an unverified result)
                    # leaves the outcome unknown, and resume will not repeat a state-changing step without you saying so
                    self.store.set_progress(task.id, number, outcome="failed" if result.error in DEFINITE_FAILURES else None)
                if not result.ok and not step.continue_on_error:
                    report.ok, report.failed_step = False, number
                    report.message = f"'{task.name}' stopped at step {number} ({step.tool}): {result.message}"
                    self.notify(task.name, report.message)
                    break
            if report.ok:
                report.message = f"'{task.name}' finished ({len(task.steps)} step(s))."
                if task.is_workflow:
                    self.store.clear_progress(task.id)
            elif task.is_workflow and report.failed_step:
                report.message += f" Say 'resume {task.name}' to continue from step {report.failed_step}."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception("automation %s crashed", task.name)
            report.ok, report.message = False, f"'{task.name}' failed unexpectedly: {exc}"
            self.notify(task.name, report.message)
        finally:
            self._running.discard(task.id)
            self._started.pop(task.id, None)
        once = isinstance(task.trigger, OnceTrigger)
        if not task.is_workflow:
            next_run = None if once else self.next_run(task)
            self.store.record_run(task.id, "ok" if report.ok else report.message, next_run, disable=once)
        else:
            self.store.record_run(task.id, "ok" if report.ok else report.message, None)
        log_event(_log, "automation_finished", name=task.name, ok=report.ok, failed_step=report.failed_step)
        if self.on_run is not None:
            try:
                self.on_run(report)
            except Exception:
                _log.warning("automation report callback failed", exc_info=True)
        return report
