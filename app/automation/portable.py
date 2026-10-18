"""Portable workflows: export one to a file, import one from a file - without ever giving it more power than it declares.

A workflow file is plain data (JSON), never code. It carries a format name, a schema version, the steps, the tools it
needs, the highest risk it reaches and how many steps would ask for approval. On import:

* the file is size-limited and strictly checked (unknown fields are rejected, steps validated like any saved workflow);
* every tool it uses must exist HERE, its arguments must still be valid, and forbidden tools (those that create other
  automations) are refused;
* what the file DECLARES must cover what it DOES: a step using a tool that isn't listed, or a risk above the declared
  maximum, makes the import fail (so a file can't quietly do more than it says);
* it is saved as a manual workflow - the file's trigger is never imported, so nothing is scheduled implicitly - and it
  is never run by importing. Running it later goes through the normal permissions and confirmations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from app import __version__
from app.automation.tasks import MAX_STEPS, Step, check_name, validate_steps
from app.security.permissions import RiskLevel
from app.tools.base import ToolError

FORMAT = "jarvis-workflow"
SCHEMA = 1
MAX_BYTES = 64_000
_RISK = {"safe": 0, "moderate": 1, "dangerous": 2}
_KEYS = {"format", "schema", "name", "description", "expected_outcome", "jarvis_version", "exported_at", "steps",
         "requires"}
_REQUIRES = {"tools", "max_risk", "asks_for_approval", "network"}


@dataclass
class ImportReport:
    ok: bool
    name: str = ""
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    max_risk: str = "safe"

    def text(self) -> str:
        if self.ok:
            extra = ("; " + "; ".join(self.warnings)) if self.warnings else ""
            return f"'{self.name}' can be imported: {len(self.steps)} step(s), highest risk {self.max_risk}{extra}."
        return "Not imported: " + "; ".join(self.problems)


def _level_name(level: RiskLevel) -> str:
    return {0: "safe", 1: "moderate", 2: "dangerous"}.get(int(level), "blocked")


def export_document(task: Any, registry: Any, ctx: Any, description: str = "", expected: str = "") -> dict[str, Any]:
    tools: list[str] = []
    highest = RiskLevel.SAFE
    network = False
    asks = 0
    for step in task.steps:
        if step.type != "tool" or not step.tool:
            continue
        if step.tool not in tools:
            tools.append(step.tool)
        tool = registry.get(step.tool)
        if tool is None:
            continue
        try:
            level = tool.level_for(tool.args_model.model_validate(step.arguments), ctx)
        except Exception:
            level = tool.level
        highest = max(highest, level)
        asks += int(level >= RiskLevel.MODERATE)
        network = network or bool(getattr(tool, "needs_network", False))
    return {"format": FORMAT, "schema": SCHEMA, "name": task.name, "description": description[:300],
            "expected_outcome": expected[:300], "jarvis_version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "requires": {"tools": tools, "max_risk": _level_name(highest), "asks_for_approval": asks,
                         "network": network},
            "steps": [s.model_dump(mode="json", exclude_defaults=True) | {"type": s.type} for s in task.steps]}


def validate_import(text: str, registry: Any, ctx: Any) -> ImportReport:
    report = ImportReport(False)
    if len(text.encode("utf-8", errors="ignore")) > MAX_BYTES:
        report.problems.append(f"the file is larger than {MAX_BYTES // 1000} KB")
        return report
    try:
        doc = json.loads(text)
    except ValueError:
        report.problems.append("it is not a valid workflow file (not JSON)")
        return report
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        report.problems.append("it is not a Jarvis workflow file")
        return report
    if doc.get("schema") != SCHEMA:
        report.problems.append(f"schema {doc.get('schema')!r} is not supported here (this version reads schema {SCHEMA})")
        return report
    extra = set(doc) - _KEYS
    requires = doc.get("requires")
    if extra:
        report.problems.append(f"unknown field(s): {', '.join(sorted(map(str, extra))[:3])}")
    if not isinstance(requires, dict) or set(requires) - _REQUIRES or "tools" not in requires or "max_risk" not in requires:
        report.problems.append("the permissions it needs are not declared correctly")
    raw_steps = doc.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > MAX_STEPS:
        report.problems.append(f"it needs 1 to {MAX_STEPS} steps")
    if report.problems:
        return report
    try:
        report.name = check_name(str(doc.get("name", "")))
        steps = [Step.model_validate(s) for s in raw_steps]
        validate_steps(steps, registry, ctx)
    except ToolError as exc:
        report.problems.append(str(exc))
        return report
    except ValidationError as exc:
        report.problems.append(f"a step is invalid: {exc.errors()[0]['msg']}")
        return report
    declared_tools = {str(t) for t in requires.get("tools", [])}
    declared_risk = _RISK.get(str(requires.get("max_risk")), -1)
    if declared_risk < 0:
        report.problems.append("the declared maximum risk is not safe, moderate or dangerous")
        return report
    highest = RiskLevel.SAFE
    for number, step in enumerate(steps, 1):
        if step.type != "tool":
            continue
        if step.tool not in declared_tools:
            report.problems.append(f"step {number} uses '{step.tool}', which the file does not declare")
            continue
        tool = registry.get(step.tool)
        try:
            level = tool.level_for(tool.args_model.model_validate(step.arguments), ctx)
        except Exception:
            level = tool.level
        highest = max(highest, level)
        if int(level) > declared_risk:
            report.problems.append(f"step {number} ({step.tool}) is {_level_name(level)}, above the declared "
                                   f"maximum ({requires.get('max_risk')})")
    if report.problems:
        return report
    report.max_risk = _level_name(highest)
    version = str(doc.get("jarvis_version", ""))
    if version and version.split(".")[:1] != __version__.split(".")[:1]:
        report.warnings.append(f"made with Jarvis {version}; this is {__version__}")
    if highest >= RiskLevel.MODERATE:
        report.warnings.append("some steps will ask for your approval when it runs")
    report.ok, report.steps = True, steps
    return report
