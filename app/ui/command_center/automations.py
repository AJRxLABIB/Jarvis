"""The automation view's data: a routine broken into Trigger -> Conditions -> Actions -> Approvals -> If it fails, with
warnings for anything that touches private data or changes the PC, an Inspect/Test that never runs a tool, and an
Enable that is refused while the routine has a real problem (and asks for an explicit tick when it has warnings).

It sits on the existing engine (app/automation/*): the same steps, the same dry run, the same simulator, the same store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.automation import studio
from app.automation.tasks import AutomationStore, AutomationTask

SENSITIVE_CATEGORIES = frozenset({"phone", "clipboard", "screen", "vision", "finance", "personal", "notes"})
SENSITIVE_NAME = re.compile(r"email|mail|message|whatsapp|contact|calendar|recall|history|clipboard|screen", re.I)
CONDITION_TEXT = {"previous_ok": "only if the step before worked", "previous_failed": "only if the step before failed",
                  "weekday": "only on weekdays", "weekend": "only on weekends", "on_battery": "only on battery",
                  "plugged_in": "only when plugged in"}


@dataclass(frozen=True)
class ActionRow:
    n: int
    text: str
    risk: str
    asks: bool
    condition: str
    on_error: str
    touches_private_data: bool
    changes_state: bool
    problem: str = ""

    @property
    def flags(self) -> str:
        parts = [self.risk]
        if self.asks:
            parts.append("asks first")
        if self.touches_private_data:
            parts.append("private data")
        if self.changes_state:
            parts.append("changes this PC")
        return ", ".join(parts)


@dataclass
class Inspection:
    name: str
    enabled: bool
    trigger: str
    kind: str                                            # manual | scheduled | event
    conditions: list[str] = field(default_factory=list)
    actions: list[ActionRow] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    failure: str = ""
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    last_status: str = ""

    @property
    def can_enable(self) -> bool:
        return not self.problems


def _kind(task: AutomationTask) -> str:
    t = task.trigger.type
    return "manual" if t == "manual" else "event" if t in ("event", "battery") else "scheduled"


def inspect(task: AutomationTask, registry, ctx, permissions, *, now: datetime | None = None,
            plugged: bool | None = None) -> Inspection:
    """Read-only. Never executes a tool."""
    now = now or datetime.now().astimezone()
    reports = studio.dry_run(task, registry, ctx, permissions, weekday=now.weekday() < 5, plugged=plugged)
    info = Inspection(task.name, task.enabled, task.trigger.describe(), _kind(task), last_status=task.last_status or "")
    for step, rep in zip(task.steps, reports):
        tool = registry.get(step.tool or "") if step.type == "tool" else None
        private = bool(tool and (tool.category in SENSITIVE_CATEGORIES or SENSITIVE_NAME.search(tool.name)))
        changes = bool(tool and (tool.risk is not None or int(tool.level) > 0))
        info.actions.append(ActionRow(rep.number, rep.what, rep.risk, rep.asks, rep.condition,
                                      "keeps going" if step.continue_on_error else "stops here", private, changes,
                                      rep.problem))
        if step.when:
            info.conditions.append(f"step {rep.number}: {CONDITION_TEXT.get(step.when, step.when)}")
        if rep.asks:
            info.approvals.append(f"step {rep.number} asks you: {rep.what}")
        if rep.problem:
            info.problems.append(f"step {rep.number}: {rep.problem}")
    if not info.conditions:
        info.conditions.append("no conditions: every step runs")
    if info.approvals and info.kind != "manual":
        info.warnings.append("A step asks for approval but this routine can start by itself: when nobody answers, the "
                             "run stops at that step.")
    if any(a.touches_private_data for a in info.actions):
        info.warnings.append("This routine reads private data (messages, mail, calendar, clipboard, screen or phone).")
    if any(a.changes_state for a in info.actions):
        info.warnings.append("This routine changes things on this PC or your devices.")
    if info.kind == "event":
        info.warnings.append("It starts on an event, so it can run when you aren't looking (at most 12 runs an hour).")
    errors = sum(1 for a in info.actions if a.on_error == "keeps going")
    info.failure = ("If a step fails the routine stops and tells you; say 'resume' to continue from that step."
                    if not errors else f"{errors} step(s) keep going after an error; the others stop the routine.")
    return info


def test_text(task: AutomationTask, registry, ctx, permissions, *, now: datetime | None = None,
              plugged: bool | None = None) -> str:
    """The dry run: what each step would do right now. Nothing runs."""
    now = now or datetime.now().astimezone()
    reports = studio.dry_run(task, registry, ctx, permissions, weekday=now.weekday() < 5, plugged=plugged)
    return "Test (nothing was run):\n" + studio.describe(reports)


def simulate_text(task: AutomationTask, what_if: str, registry=None) -> str:
    """A what-if with pretend tools on a scratch database (a step fails, a crash, a double trigger...)."""
    from app.automation import simulator

    steps = [{"tool": s.tool, "arguments": s.arguments} if s.type == "tool" else {"type": "notify", "message": s.message}
             for s in task.steps]
    target = next((s.tool for s in task.steps if s.type == "tool"), "")
    return simulator.simulate_workflow(steps, simulator.scenario_for(what_if, target or ""), registry).describe()


def enable(store: AutomationStore, task: AutomationTask, info: Inspection, *, acknowledged: bool,
           now: datetime | None = None) -> tuple[bool, str]:
    """Turn a routine on. Refused while it has a real problem; with warnings it needs ``acknowledged``."""
    if info.problems:
        return False, "Not enabled: " + "; ".join(info.problems)
    if info.warnings and not acknowledged:
        return False, "Read the warnings and tick 'I've read the warnings' to enable it."
    store.set_enabled(task.id, True, task.trigger.next_run(now or datetime.now().astimezone()))
    return True, f"'{task.name}' is on."


def disable(store: AutomationStore, task: AutomationTask) -> str:
    store.set_enabled(task.id, False, None)
    return f"'{task.name}' is off."
