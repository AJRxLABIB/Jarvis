"""Skill card: one honest page about a saved routine (workflow / skill) - what it needs, what it may do, how it fails, how it can be undone, how it has been doing.

Built only from what already exists (the saved steps and inputs in ``automation/tasks.py``, versions, the tool registry's risk levels, undo support in
``core/undo.py``, run history in ``automation/reliability.py``). Nothing is invented: a thing the routine does not record (an expected result, an
explicit verification) is listed under ``gaps`` instead of being filled in. Read-only; it never runs a step. The visual editing (dry run, move / remove /
condition / continue-on-error edits, version restore) is the existing Workflow Studio (``automation/studio.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.security.permissions import RiskLevel

# tools whose successful calls ``core/undo.inverse_of`` can reverse (kept in step with that function by a test)
UNDOABLE_TOOLS = frozenset({"move_item", "rename_item", "batch_rename", "edit_code", "zip_items", "extract_zip", "merge_pdfs", "convert_image",
                            "word_to_pdf", "create_item", "volume_control", "brightness_control", "device_toggle"})
_APP_TOOLS = {"open_application": "name", "open_app": "name"}
_RISK = {0: "safe", 1: "moderate", 2: "dangerous", 3: "blocked"}


@dataclass
class StepInfo:
    number: int
    what: str
    tool: str
    risk: str
    asks: bool
    undoable: bool
    on_failure: str
    condition: str


@dataclass
class SkillCard:
    name: str
    trigger: str
    enabled: bool
    version: int
    inputs: dict[str, dict[str, Any]]
    steps: list[StepInfo]
    needs_tools: list[str]
    needs_apps: list[str]
    needs_capabilities: list[str]
    highest_risk: str
    runs: int = 0
    succeeded: int = 0
    failing_streak: int = 0
    last_failure: str = ""
    gaps: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Skill '{self.name}' (version {self.version}, {'on' if self.enabled else 'paused'}) - runs: {self.trigger}",
                 f"Highest risk of any step: {self.highest_risk}. Needs tools: {', '.join(self.needs_tools) or 'none'}"
                 + (f"; apps: {', '.join(self.needs_apps)}" if self.needs_apps else "") + f"; abilities: {', '.join(self.needs_capabilities) or 'none'}."]
        if self.inputs:
            lines.append("Inputs: " + "; ".join(f"{k} (default {v.get('default')!r})" if v.get("default") not in (None, "") else f"{k} (required)"
                                                for k, v in self.inputs.items()))
        for s in self.steps:
            tail = ["asks you first" if s.asks else "", "can be undone" if s.undoable else "cannot be undone automatically",
                    f"if it fails: {s.on_failure}", s.condition]
            lines.append(f"  {s.number}. {s.what} [{s.risk}] - " + "; ".join(t for t in tail if t))
        if self.runs:
            lines.append(f"History: {self.succeeded} of {self.runs} recent runs succeeded" + (f"; the last {self.failing_streak} failed ({self.last_failure})"
                                                                                             if self.failing_streak else "") + ".")
        else:
            lines.append("History: it has not run yet.")
        if self.gaps:
            lines.append("Not recorded: " + "; ".join(self.gaps))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["steps"] = [s.__dict__ for s in self.steps]
        return d


def build(task: Any, registry: Any, db: Any = None) -> SkillCard:
    """``task`` is an AutomationTask; ``registry`` the tool registry (risk and category); ``db`` adds versions and run history when given."""
    steps: list[StepInfo] = []
    tools: list[str] = []
    apps: list[str] = []
    caps: list[str] = []
    top = 0
    for i, step in enumerate(task.steps, 1):
        if step.type == "notify":
            steps.append(StepInfo(i, f'say "{step.message}"', "notify", "safe", False, False, "continues" if step.continue_on_error else "stops here",
                                  step.when.replace("_", " ") if step.when else ""))
            continue
        tool = registry.get(step.tool) if registry is not None else None
        level = int(tool.level) if tool is not None else 3
        top = max(top, level)
        if step.tool not in tools:
            tools.append(step.tool)
        if tool is not None and tool.category not in caps:
            caps.append(tool.category)
        key = _APP_TOOLS.get(step.tool or "")
        if key and isinstance(step.arguments.get(key), str) and step.arguments[key] not in apps:
            apps.append(step.arguments[key][:40])
        steps.append(StepInfo(i, step.describe(), step.tool or "?", _RISK.get(level, "blocked") if tool is not None else "unknown tool",
                              level >= int(RiskLevel.MODERATE), (step.tool in UNDOABLE_TOOLS), "continues" if step.continue_on_error else "stops here",
                              f"only if {step.when.replace('_', ' ')}" if step.when else ""))
    card = SkillCard(task.name, task.trigger.describe(), task.enabled, 1, dict(task.parameters), steps, tools, apps, caps, _RISK.get(top, "safe"))
    if db is not None:
        try:
            revisions = task_revisions(db, task.name)
            card.version = (max(r["version"] for r in revisions) + 1) if revisions else 1
        except Exception:
            pass
        try:
            from app.automation import reliability

            st = reliability.stats(db, task.name)
            card.runs, card.succeeded, card.failing_streak, card.last_failure = st.runs, st.ok, st.streak_failures, st.last_failure
        except Exception:
            pass
    card.gaps = ["an expected result (what 'done' looks like) - success is each step's own check passing",
                 "an explicit end-to-end verification step"]
    if any(not s.undoable and s.risk != "safe" for s in steps):
        card.gaps.append("undo for: " + ", ".join(s.tool for s in steps if not s.undoable and s.risk != "safe"))
    return card


def task_revisions(db: Any, name: str) -> list[dict[str, Any]]:
    from app.automation.tasks import AutomationStore

    return AutomationStore(db).revisions(name)
