"""Workflow studio logic (no UI): dry run, safe step edits and version restore for saved automations/workflows.

* ``dry_run`` shows what a run WOULD do right now - each step's tool, risk, whether it would ask you, whether its
  condition holds - without running anything or touching any file.
* ``edit_steps`` applies one change (move, remove, set a condition, toggle "continue on error", add a pause message) and
  returns the new list; ``save`` validates the whole list the same way ``schedule_task`` does before storing it (the old
  version is kept and can be restored).
* The trigger (when it runs) is never changed here, and nothing in this module runs a step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.automation.tasks import MAX_STEPS, AutomationStore, AutomationTask, Step, parse_steps, validate_steps
from app.security.permissions import RiskLevel
from app.tools.base import ToolError

CONDITIONS = (None, "previous_ok", "previous_failed", "weekday", "weekend", "on_battery", "plugged_in")
_RISK_NAMES = {0: "safe", 1: "moderate", 2: "dangerous", 3: "blocked"}


@dataclass(frozen=True)
class StepReport:
    number: int
    what: str
    risk: str
    asks: bool          # would it ask you before running?
    condition: str
    would_run: str      # "yes" | "skipped (condition)" | "depends on the earlier steps"
    problem: str = ""


def _condition_holds(when: str | None, *, weekday: bool, plugged: bool | None) -> str:
    if when is None:
        return "yes"
    if when in ("previous_ok", "previous_failed"):
        return "depends on the earlier steps"
    if when in ("weekday", "weekend"):
        return "yes" if weekday == (when == "weekday") else "skipped (condition)"
    if plugged is None:
        return "depends on the battery state"
    return "yes" if plugged == (when == "plugged_in") else "skipped (condition)"


def dry_run(task: AutomationTask, registry: Any, ctx: Any, permissions: Any, *, weekday: bool,
            plugged: bool | None) -> list[StepReport]:
    """What each step would do right now. Never executes a tool."""
    from app.security.permissions import Decision

    reports = []
    for number, step in enumerate(task.steps, start=1):
        condition = step.when.replace("_", " ") if step.when else ""
        would = _condition_holds(step.when, weekday=weekday, plugged=plugged)
        if step.type == "notify":
            reports.append(StepReport(number, f'say "{step.message}"', "safe", False, condition, would))
            continue
        tool = registry.get(step.tool or "")
        if tool is None:
            reports.append(StepReport(number, step.describe(), "unknown", False, condition, would,
                                      f"there is no tool named '{step.tool}' any more"))
            continue
        try:
            args = tool.args_model.model_validate(step.arguments)
        except Exception as exc:
            reports.append(StepReport(number, step.describe(), "unknown", False, condition, would,
                                      f"arguments no longer valid: {str(exc).splitlines()[0][:80]}"))
            continue
        try:
            level = tool.level_for(args, ctx)
        except ToolError:
            level = tool.level
        decision, final = permissions.decide(tool.name, level)
        problem = "blocked by your permission settings" if int(final) >= int(RiskLevel.CRITICAL) else ""
        reports.append(StepReport(number, step.describe(), _RISK_NAMES.get(int(final), "?"),
                                  decision == Decision.CONFIRM, condition, would, problem))
    return reports


def describe(reports: list[StepReport]) -> str:
    lines = []
    for r in reports:
        flags = [r.risk] + (["asks first"] if r.asks else []) + ([f"only if {r.condition}"] if r.condition else [])
        extra = f" PROBLEM: {r.problem}" if r.problem else ""
        lines.append(f"{r.number}. {r.what} [{', '.join(flags)}] -> {r.would_run}.{extra}")
    if any(r.asks for r in reports):
        lines.append("Steps that ask first are cancelled when nobody answers, so an unattended run stops there.")
    return "\n".join(lines)


def edit_steps(steps: list[Step], action: str, index: int, value: str | None = None) -> list[Step]:
    """One change to a copy of the list. ``index`` is 0-based."""
    steps = [s.model_copy(deep=True) for s in steps]
    if not 0 <= index < len(steps):
        raise ToolError("That step doesn't exist.", "invalid_steps")
    if action == "up":
        if index > 0:
            steps[index - 1], steps[index] = steps[index], steps[index - 1]
    elif action == "down":
        if index < len(steps) - 1:
            steps[index + 1], steps[index] = steps[index], steps[index + 1]
    elif action == "remove":
        if len(steps) == 1:
            raise ToolError("A workflow needs at least one step; delete the workflow instead.", "invalid_steps")
        del steps[index]
    elif action == "condition":
        if value not in CONDITIONS and value != "":
            raise ToolError("Unknown condition.", "invalid_steps")
        steps[index].when = value or None
    elif action == "continue_on_error":
        steps[index].continue_on_error = not steps[index].continue_on_error
    elif action == "add_note":
        if len(steps) >= MAX_STEPS:
            raise ToolError(f"At most {MAX_STEPS} steps.", "invalid_steps")
        steps.insert(index + 1, Step(type="notify", message=(value or "").strip()[:300] or None))
    else:
        raise ToolError("Unknown edit.", "invalid_steps")
    return steps


def save(store: AutomationStore, task: AutomationTask, steps: list[Step], registry: Any, ctx: Any) -> AutomationTask:
    """Validate like schedule_task does, then store. The trigger and the next run time are kept exactly."""
    parse_steps([s.model_dump(mode="json", exclude_defaults=True) | {"type": s.type} for s in steps])
    validate_steps(steps, registry, ctx)
    next_run = None
    if task.next_run_at:
        from datetime import datetime

        next_run = datetime.fromisoformat(task.next_run_at)
    saved, _ = store.save(task.name, task.trigger, steps, next_run)
    if not task.enabled:  # saving must not switch a paused automation back on
        store.set_enabled(saved.id, False, None)
        saved = store.get(saved.id) or saved
    return saved


def previous_versions(store: AutomationStore, name: str) -> list[dict[str, Any]]:
    return store.revisions(name)
