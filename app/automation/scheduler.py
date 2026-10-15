"""Runs automations while the assistant is open.

* Checks every 30 s (cheap: one small SQLite query), so it costs ~nothing when idle.
* Every tool step goes through the normal ToolExecutor: validation, permissions,
  confirmation (a dialog nobody answers is cancelled after 60 s), rate limit, audit.
* A failed step stops the run (unless the step says continue_on_error).
* Missed while the app was closed: one-off tasks up to 1 hour late still run;
  older ones are marked missed. Repeating tasks just continue from now.
"""

from __future__ import annotations

import asyncio
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

    async def catch_up(self) -> None:
        """Deal with runs that were due while the assistant was closed."""
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
                    self.notify("Missed", f"'{task.name}' was due at {due:%H:%M} while the assistant was closed.")
                # else: tick() runs it now, a little late
            else:
                self.store.record_run(task.id, task.last_status or "skipped while closed", self.next_run(task, now))

    async def tick(self) -> list[RunReport]:
        from app.core import emergency

        if emergency.STATE.flags.get("automation_disabled"):
            return []  # emergency controls: automations are off until you turn them on in the window
        now = self.clock()
        self.last_tick_at = now
        reports = []
        tasks = self.store.all()
        happened = self._events_now() if any(isinstance(t.trigger, EventTrigger) and t.trigger.event != "file_changed" and t.enabled
                                             for t in tasks) else None
        for task in tasks:
            if not task.enabled or task.id in self._running:
                continue
            if isinstance(task.trigger, BatteryTrigger):
                if self._battery_due(task):
                    reports.append(await self.run(task))
                continue
            if isinstance(task.trigger, EventTrigger) and task.trigger.event == "file_changed":
                if self._files_due(task):
                    reports.append(await self.run(task))
                continue
            if isinstance(task.trigger, EventTrigger):
                if happened is not None and self._event_due(task.trigger, *happened):
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

    async def run(self, task: AutomationTask, *, manual: bool = False) -> RunReport:
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
            return await self._run(task, manual=manual)

    async def _run(self, task: AutomationTask, *, manual: bool = False) -> RunReport:
        self._running.add(task.id)
        report = RunReport(task, ok=True)
        try:
            if isinstance(task.trigger, OnceTrigger) and not task.is_workflow and not manual:
                # at-most-once: claim a one-time task BEFORE its steps run, so a crash mid-run can never repeat them
                self.store.record_run(task.id, "running", None, disable=True)
            log_event(_log, "automation_started", name=task.name, manual=manual)
            previous_ok: bool | None = None
            from app.core import emergency

            for number, step in enumerate(task.steps, start=1):
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
                if not result.ok and not step.continue_on_error:
                    report.ok, report.failed_step = False, number
                    report.message = f"'{task.name}' stopped at step {number} ({step.tool}): {result.message}"
                    self.notify(task.name, report.message)
                    break
            if report.ok:
                report.message = f"'{task.name}' finished ({len(task.steps)} step(s))."
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
