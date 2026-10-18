"""Workflow simulator: run a workflow's REAL scheduling and recovery logic against pretend tools.

``dry_run`` (studio.py) shows what a run would do right now. The simulator goes further: it plays a workflow through
failures, timeouts, a lost response, a crash and restart, a duplicate trigger, an emergency stop and the passing of time -
so you can see how it behaves before it ever touches anything real.

How it stays safe and honest:

* It uses the real ``Scheduler`` (checkpoints, overlap refusal, emergency stop, resume) but a **scratch database in a
  temporary folder** that is deleted afterwards - your automations, memories and settings are never read or written.
* The tools are **scripted stand-ins**. No real tool handler is called, so nothing is opened, sent, saved or deleted. The
  real tool list is only consulted to check that each named tool exists and what its risk level is.
* Every result is labelled SIMULATED. "Effects" are what WOULD have happened if the tool behaved as scripted; they are not
  evidence that the real tool behaves that way. A simulation can find a design problem (a step that would run twice after
  a crash); it cannot prove a real service works.

What it can show you, for example: a step that is not idempotent being applied twice because a crash happened after the
effect but before the step was checkpointed - exactly the case the real engine resumes AT, by design.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import TypeAdapter

from app.agent.tool_manager import ToolExecution
from app.ai.base import ToolCall
from app.automation.scheduler import Scheduler
from app.automation.tasks import AutomationStore, Step, parse_steps
from app.automation.triggers import ManualTrigger, Trigger
from app.memory.database import Database
from app.tools.base import ToolResult

BANNER = "SIMULATION - nothing real was executed; every result below is hypothetical."
OUTCOMES = ("ok", "fail", "timeout", "denied", "lost_response")


@dataclass
class Behaviour:
    """How a pretend tool behaves.

    ok            works: its effect is applied and the step succeeds
    fail          fails cleanly: no effect
    timeout       fails with a timeout: no effect (the caller cannot know that)
    denied        permission refused: no effect
    lost_response the effect IS applied but the answer never arrives, so the step looks failed (the ambiguous case)
    ``fail_times`` makes the first N calls fail before the behaviour above applies (retry scenarios).
    ``idempotent`` means applying the same call twice has no second effect.
    """

    outcome: str = "ok"
    fail_times: int = 0
    idempotent: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")


@dataclass
class Scenario:
    behaviours: dict[str, Behaviour] = field(default_factory=dict)       # per tool name; others default to ok
    crash_on_tool: str | None = None        # the process "dies" during the first call of this tool
    crash_after_effect: bool = True         # ...after its effect was applied (False = before)
    restart_after_crash: bool = True        # start a new Scheduler on the same database and resume
    duplicate_trigger: bool = False         # the same workflow is triggered twice at once
    stop_before_tool: str | None = None     # the emergency stop is pressed just before this tool's first call
    resumes: int = 0                        # how many times "resume" is said after a failed run
    on_uncertain: str | None = None         # what you tell resume about a step whose outcome is unknown: skip | repeat


@dataclass
class SimReport:
    banner: str = BANNER
    problems: list[str] = field(default_factory=list)      # the workflow could not be simulated (unknown tool ...)
    timeline: list[str] = field(default_factory=list)
    effects: dict[str, int] = field(default_factory=dict)  # "tool(args)" -> how many times it WOULD have taken effect
    duplicated: list[str] = field(default_factory=list)    # effects applied more than once although not idempotent
    final_ok: bool | None = None
    runs: list[str] = field(default_factory=list)          # when a scheduled simulation ran (simulated time)

    def describe(self) -> str:
        lines = [self.banner, *[f"! {p}" for p in self.problems], *self.timeline]
        if self.effects:
            lines.append("Effects that WOULD have happened (hypothetical): " + "; ".join(
                f"{k} x{v}" for k, v in self.effects.items()))
        if self.duplicated:
            lines.append("DESIGN PROBLEM - applied more than once and not idempotent: " + "; ".join(self.duplicated)
                         + ". Make the step idempotent or check before repeating it.")
        if self.final_ok is not None:
            lines.append(f"Simulated final state: {'completed' if self.final_ok else 'not completed'}.")
        return "\n".join(lines)


class SimCrash(asyncio.CancelledError):
    """The pretend process died here. It is a CancelledError so the scheduler treats it as the run being cut off."""


class _SimExecutor:
    def __init__(self, scenario: Scenario, report: SimReport, database: Database, registry: Any = None) -> None:
        self.scenario, self.report = scenario, report
        self.registry = registry          # read only: the scheduler asks it how risky a step's tool is
        self.context = SimpleNamespace(database=database)
        self.calls: dict[str, int] = {}
        self.ledger: dict[str, int] = {}
        self.crashed = False
        self.stopped = False

    def _key(self, call: ToolCall) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in sorted(call.arguments.items()))
        return f"{call.name}({args})"

    def _apply(self, call: ToolCall, behaviour: Behaviour) -> None:
        key = self._key(call)
        if behaviour.idempotent and key in self.ledger:
            return
        self.ledger[key] = self.ledger.get(key, 0) + 1

    async def execute(self, call: ToolCall) -> ToolExecution:
        from app.core import emergency

        await asyncio.sleep(0)   # a real tool waits for something; yielding here lets overlapping runs interleave as they would
        name = call.name
        sc = self.scenario
        if sc.stop_before_tool == name and not self.stopped:
            self.stopped = True
            emergency.STATE.flags["automation_disabled"] = True
            self.report.timeline.append(f"[emergency stop pressed before {name}]")
            return ToolExecution(call, ToolResult.failure("blocked", "stopped by the emergency controls (simulated)"))
        self.calls[name] = self.calls.get(name, 0) + 1
        behaviour = sc.behaviours.get(name, Behaviour())
        n = self.calls[name]
        label = self._key(call)
        if sc.crash_on_tool == name and not self.crashed:
            self.crashed = True
            if sc.crash_after_effect:
                self._apply(call, behaviour)
            self.report.timeline.append(f"[process dies during {label}; effect "
                                        f"{'was' if sc.crash_after_effect else 'was NOT'} applied]")
            raise SimCrash()
        if n <= behaviour.fail_times:
            self.report.timeline.append(f"{label}: simulated failure (call {n} of {behaviour.fail_times} scripted failures)")
            return ToolExecution(call, ToolResult.failure("invalid_arguments", "simulated failure before doing anything"))
        outcome = behaviour.outcome
        if outcome == "ok":
            self._apply(call, behaviour)
            self.report.timeline.append(f"{label}: ok (simulated)")
            return ToolExecution(call, ToolResult.success("simulated success"))
        if outcome == "lost_response":
            self._apply(call, behaviour)
            self.report.timeline.append(f"{label}: effect applied but the response was lost (simulated) - looks failed")
            return ToolExecution(call, ToolResult.failure("timeout", "no response (simulated)"))
        # the real engine's failure codes: a clean refusal is DEFINITE (nothing happened); a timeout leaves it unknown
        code, message = {"fail": ("invalid_arguments", "simulated failure before doing anything"),
                         "timeout": ("timeout", "timed out (simulated)"),
                         "denied": ("permission_error", "permission denied (simulated)")}[outcome]
        self.report.timeline.append(f"{label}: {message}")
        return ToolExecution(call, ToolResult.failure(code, message))


def _check_tools(steps: list[Step], registry: Any, report: SimReport) -> None:
    for number, step in enumerate(steps, start=1):
        if step.type != "tool":
            continue
        tool = registry.get(step.tool or "") if registry is not None else True
        if tool is None:
            report.problems.append(f"step {number}: there is no tool called '{step.tool}'")
        elif registry is not None:
            report.timeline.append(f"step {number}: {step.tool} (risk level {int(tool.level)}) will be SIMULATED, not run")


def _changes_something(registry: Any, effect_key: str) -> bool:
    """Doing a read-only tool twice is harmless, so only state-changing tools count as a duplicate problem."""
    tool = registry.get(effect_key.split("(", 1)[0]) if registry is not None else None
    return tool is None or tool.risk is not None or int(tool.level) > 0


def _finish(report: SimReport, executor: _SimExecutor) -> SimReport:
    report.effects = dict(executor.ledger)
    report.duplicated = [k for k, v in executor.ledger.items() if v > 1 and _changes_something(executor.registry, k)]
    return report


WHAT_IFS = ("normal", "tool_fails", "tool_times_out", "permission_denied", "response_lost", "crash_and_restart",
            "duplicate_trigger", "emergency_stop")
WHAT_IF_LABELS = {"normal": "Everything works", "tool_fails": "A step fails", "tool_times_out": "A step times out",
                  "permission_denied": "A step is denied", "response_lost": "A step's answer is lost",
                  "crash_and_restart": "Jarvis crashes, then restarts", "duplicate_trigger": "It is triggered twice at once",
                  "emergency_stop": "The emergency stop is pressed"}


def scenario_for(what_if: str, target: str, resumes: int = 1, idempotent: bool = False,
                 on_uncertain: str | None = None) -> Scenario:
    """The scenario for one named what-if aimed at the step that uses tool ``target`` (shared by the tool and the studio)."""
    if what_if not in WHAT_IFS:
        raise ValueError(f"unknown what-if '{what_if}'")
    scenario = Scenario(resumes=resumes, on_uncertain=on_uncertain)
    behaviour = Behaviour(idempotent=idempotent)
    if what_if == "tool_fails":
        behaviour = Behaviour("fail", idempotent=idempotent)
    elif what_if == "tool_times_out":
        behaviour = Behaviour("timeout", idempotent=idempotent)
    elif what_if == "permission_denied":
        behaviour = Behaviour("denied")
    elif what_if == "response_lost":
        behaviour = Behaviour("lost_response", idempotent=idempotent)
    elif what_if == "crash_and_restart":
        scenario.crash_on_tool = target
    elif what_if == "duplicate_trigger":
        scenario.duplicate_trigger = True
    elif what_if == "emergency_stop":
        scenario.stop_before_tool = target
    scenario.behaviours = {target: behaviour}
    return scenario


def simulate_workflow(raw_steps: Any, scenario: Scenario | None = None, registry: Any = None) -> SimReport:
    """Play one workflow through ``scenario``. ``raw_steps`` is the same list a saved workflow uses."""
    return asyncio.run(_simulate_workflow(parse_steps(raw_steps), scenario or Scenario(), registry))


async def _simulate_workflow(steps: list[Step], scenario: Scenario, registry: Any) -> SimReport:
    from app.core import emergency

    report = SimReport()
    _check_tools(steps, registry, report)
    if report.problems:
        return report
    saved_flags = dict(emergency.STATE.flags)
    emergency.STATE.flags["automation_disabled"] = False
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-sim-") as folder:
            db = Database(Path(folder) / "sim.db")
            db.initialize()
            store = AutomationStore(db)
            task, _ = store.save("simulated workflow", ManualTrigger(), steps, None)
            executor = _SimExecutor(scenario, report, db, registry)
            notices: list[str] = []
            scheduler = Scheduler(store, executor, lambda title, msg: notices.append(msg))  # type: ignore[arg-type]
            if scenario.duplicate_trigger:
                first, second = await asyncio.gather(scheduler.run(task), scheduler.run(task))
                report.timeline.append(f"second trigger: {'started' if second.ok else 'refused - ' + second.message}")
                result = first
            else:
                try:
                    result = await scheduler.run(task)
                except SimCrash:
                    result = None
            if result is None and scenario.restart_after_crash:
                scheduler = Scheduler(store, executor, lambda title, msg: notices.append(msg))  # type: ignore[arg-type]
                report.timeline.append("[restart] a new scheduler starts on the same database")
                await scheduler.catch_up()
                progress = store.progress(task.id)
                report.timeline.append(f"checkpoint says: resume at step {progress}" if progress else "no checkpoint was saved")
                result = await scheduler.run(task, resume=True, uncertain=scenario.on_uncertain)
            for _ in range(scenario.resumes):
                if result is not None and result.ok:
                    break
                report.timeline.append("[user says: resume" + (f", {scenario.on_uncertain} the uncertain step]" if scenario.on_uncertain else "]"))
                result = await scheduler.run(task, resume=True, uncertain=scenario.on_uncertain)
            if result is not None:
                report.timeline.extend(f"  {line}" for line in result.steps)
                report.final_ok = result.ok
                if not result.ok:
                    report.timeline.append(f"result: {result.message}")
            else:
                report.final_ok = False
                report.timeline.append("the process died and was not restarted in this scenario")
            return _finish(report, executor)
    finally:
        emergency.STATE.flags.clear()
        emergency.STATE.flags.update(saved_flags)


# ---------------------------------------------------------------------------------- simulated time


def simulate_schedule(raw_steps: Any, trigger: dict[str, Any], start: datetime, days: int = 3,
                      scenario: Scenario | None = None, registry: Any = None, step_minutes: int = 15) -> SimReport:
    """Move a simulated clock forward ``days`` days and report WHEN the workflow would have run. Takes no real time."""
    return asyncio.run(_simulate_schedule(parse_steps(raw_steps), TypeAdapter(Trigger).validate_python(trigger), start, days,
                                          scenario or Scenario(), registry, step_minutes))


async def _simulate_schedule(steps: list[Step], trigger: Any, start: datetime, days: int, scenario: Scenario,
                             registry: Any, step_minutes: int) -> SimReport:
    from app.core import emergency

    report = SimReport()
    _check_tools(steps, registry, report)
    if report.problems:
        return report
    saved_flags = dict(emergency.STATE.flags)
    emergency.STATE.flags["automation_disabled"] = False
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-sim-") as folder:
            db = Database(Path(folder) / "sim.db")
            db.initialize()
            store = AutomationStore(db)
            clock = {"now": start}
            executor = _SimExecutor(scenario, report, db, registry)
            scheduler = Scheduler(store, executor, lambda t, m: None, clock=lambda: clock["now"],  # type: ignore[arg-type]
                                  battery=lambda: None, events=lambda: {"wifi": "", "apps": set(), "usb": set()})
            first = trigger.next_run(start)
            task, _ = store.save("simulated schedule", trigger, steps, first)
            report.timeline.append(f"scheduled: {trigger.describe()}; first run {first:%a %d %b %H:%M}" if first else "no run is due")
            end = start + timedelta(days=days)
            while clock["now"] < end:
                clock["now"] += timedelta(minutes=step_minutes)
                for run in await scheduler.tick():
                    report.runs.append(f"{clock['now']:%a %d %b %H:%M}")
                    report.timeline.append(f"{clock['now']:%a %d %b %H:%M}: ran ({'ok' if run.ok else run.message})")
            report.timeline.append(f"{len(report.runs)} run(s) in {days} simulated day(s)")
            report.final_ok = all("ok" in line or "ran (ok)" in line for line in report.timeline if ": ran (" in line) if report.runs else None
            return _finish(report, executor)
    finally:
        emergency.STATE.flags.clear()
        emergency.STATE.flags.update(saved_flags)
