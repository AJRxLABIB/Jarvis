"""Your own voice commands without code: a phrase and a few simple steps, made in a form.

"gaming time" -> close Chrome, open Steam, volume 70. Each step becomes an ordinary workflow step
(the same tools, validation and permissions as everything else); saying the phrase runs it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.automation.tasks import Step
from app.tools.base import ToolError


@dataclass(frozen=True)
class StepKind:
    key: str
    label: str
    hint: str  # what goes in the value box
    needs_value: bool = True


STEP_KINDS: tuple[StepKind, ...] = (
    StepKind("open_app", "Open app", "e.g. Steam"),
    StepKind("close_app", "Close app", "e.g. Chrome"),
    StepKind("open_website", "Open website", "e.g. facebook or github.com"),
    StepKind("play_music", "Play music", "song or artist (empty = any)", needs_value=False),
    StepKind("volume", "Set volume", "0-100"),
    StepKind("brightness", "Set brightness", "0-100"),
    StepKind("layout", "Window layout", "a saved layout's name, e.g. work"),
    StepKind("focus", "Focus mode", "minutes, e.g. 45"),
    StepKind("run_project", "Run my project", "project folder (empty = your project)", needs_value=False),
    StepKind("say", "Say", "what Jarvis says"),
)
KINDS = {kind.key: kind for kind in STEP_KINDS}


def _number(value: str, label: str, low: int, high: int) -> int:
    try:
        number = int(value.strip().rstrip("%"))
    except ValueError:
        raise ToolError(f"{label} needs a number from {low} to {high}.", "invalid_steps") from None
    if not low <= number <= high:
        raise ToolError(f"{label} needs a number from {low} to {high}.", "invalid_steps")
    return number


def to_step(kind: str, value: str) -> Step:
    spec = KINDS.get(kind)
    if spec is None:
        raise ToolError(f"Unknown step '{kind}'.", "invalid_steps")
    value = (value or "").strip()
    if spec.needs_value and not value:
        raise ToolError(f"'{spec.label}' needs a value ({spec.hint}).", "invalid_steps")
    arguments: dict[str, Any]
    if kind == "open_app":
        return Step(tool="open_application", arguments={"name": value})
    if kind == "close_app":
        return Step(tool="close_application", arguments={"name": value}, continue_on_error=True)
    if kind == "open_website":
        return Step(tool="open_website", arguments={"site": value})
    if kind == "play_music":
        return Step(tool="play_music", arguments={"query": value} if value else {})
    if kind == "volume":
        return Step(tool="volume_control", arguments={"action": "set", "level": _number(value, spec.label, 0, 100)})
    if kind == "brightness":
        return Step(tool="brightness_control", arguments={"action": "set", "level": _number(value, spec.label, 0, 100)},
                    continue_on_error=True)
    if kind == "layout":
        return Step(tool="arrange_windows", arguments={"action": "restore", "layout": value})
    if kind == "focus":
        return Step(tool="focus_mode", arguments={"action": "start", "minutes": _number(value, spec.label, 1, 600)})
    if kind == "run_project":
        arguments = {"project": value} if value else {}
        return Step(tool="run_project", arguments=arguments)
    return Step(type="notify", message=value[:300])


def build_steps(rows: list[tuple[str, str]]) -> list[Step]:
    steps = [to_step(kind, value) for kind, value in rows if kind]
    if not steps:
        raise ToolError("Add at least one step.", "invalid_steps")
    return steps


def command_name(text: str) -> str:
    name = " ".join((text or "").split()).strip(" .!?")
    if not 2 <= len(name) <= 40:
        raise ToolError("Give the command a name of 2 to 40 characters, e.g. 'gaming time'.", "missing_name")
    return name


def save_command(ctx, name: str, rows: list[tuple[str, str]]):
    """Validate (tools, arguments, permissions) and save as a workflow run by saying its name."""
    from app.automation.tasks import AutomationStore
    from app.automation.triggers import ManualTrigger
    from app.tools.automation import _save

    name = command_name(name)
    steps = build_steps(rows)
    if ctx.database is None:
        raise ToolError("There's no database to save commands in.", "no_database")
    existing = {task.name.lower() for task in AutomationStore(ctx.database).all()}
    if name.lower() in existing:
        raise ToolError(f"There is already an automation called '{name}'. Pick another name or delete that one.",
                        "exists")
    task, _ = _save(ctx, name, ManualTrigger(), steps)
    return task
