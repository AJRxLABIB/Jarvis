"""Automation tasks: a trigger plus a list of steps, stored in SQLite."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.agent.tool_manager import ToolRegistry
from app.automation.triggers import Trigger
from app.memory.database import Database, utc_now_iso
from app.security.permissions import RiskLevel
from app.tools.base import ToolContext, ToolError
from app.utils.redaction import redact

MAX_STEPS = 10
_NAME = re.compile(r"^[\w][\w .,:'&()\-]{0,59}$")
# Automations can't create, change or run automations (no self-replicating schedules).
FORBIDDEN_STEP_TOOLS = frozenset({"set_reminder", "schedule_task", "save_workflow", "run_workflow", "manage_automation",
                                  "list_automations", "forget_everything", "import_workflow",
                                  "export_workflow", "background_mode", "simulate_workflow", "teach_workflow"})
_TRIGGER = TypeAdapter(Trigger)


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool", "notify"] = "tool"
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    message: str | None = Field(default=None, max_length=300)
    continue_on_error: bool = False
    # run this step only if: the step before it worked / failed, it is a weekday / weekend, the PC is on battery / plugged in
    when: Literal["previous_ok", "previous_failed", "weekday", "weekend", "on_battery", "plugged_in"] | None = None

    def describe(self) -> str:
        prefix = f"[only if {self.when.replace('_', ' ')}] " if self.when else ""
        if self.type == "notify":
            return f'{prefix}say "{self.message}"'
        args = ", ".join(f"{k}={v!r}" for k, v in self.arguments.items())
        return f"{prefix}{self.tool}({args})"


@dataclass(frozen=True)
class AutomationTask:
    id: str
    name: str
    enabled: bool
    trigger: Any  # one of the trigger models
    steps: list[Step]
    last_run_at: str | None
    next_run_at: str | None
    last_status: str | None

    @property
    def is_workflow(self) -> bool:
        return self.trigger.type == "manual"

    def describe(self) -> str:
        state = "" if self.enabled else " (paused)"
        return f"{self.name}: {self.trigger.describe()} -> " + "; ".join(s.describe() for s in self.steps) + state

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "when": self.trigger.describe(), "steps": [s.describe() for s in self.steps],
                "enabled": self.enabled, "next_run": self.next_run_at, "last_status": self.last_status}


def parse_steps(raw: Any) -> list[Step]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise ToolError("Steps must be a list like [{\"tool\": \"open_application\", \"arguments\": {\"name\": \"chrome\"}}].",
                            "invalid_steps") from None
    if not isinstance(raw, list) or not raw:
        raise ToolError("Give at least one step.", "invalid_steps")
    if len(raw) > MAX_STEPS:
        raise ToolError(f"At most {MAX_STEPS} steps per automation.", "invalid_steps")
    steps = []
    for item in raw:
        if isinstance(item, dict) and "tool" in item and "type" not in item:
            item = {"type": "tool", **item}
        try:
            steps.append(Step.model_validate(item))
        except ValidationError as exc:
            raise ToolError(f"A step is invalid: {exc.errors()[0]['msg']}.", "invalid_steps") from None
    return steps


def validate_steps(steps: list[Step], registry: ToolRegistry, ctx: ToolContext) -> RiskLevel:
    """Check every step now (not only when it runs). Returns the highest risk level."""
    highest = RiskLevel.SAFE
    for number, step in enumerate(steps, start=1):
        if step.type == "notify":
            if not step.message or not step.message.strip():
                raise ToolError(f"Step {number} needs a message.", "invalid_steps")
            if redact(step.message) != step.message:
                raise ToolError("Reminders can't contain secrets.", "sensitive")
            continue
        if not step.tool:
            raise ToolError(f"Step {number} needs a tool.", "invalid_steps")
        if step.tool in FORBIDDEN_STEP_TOOLS:
            raise ToolError(f"Step {number}: automations can't use '{step.tool}'.", "invalid_steps")
        tool = registry.get(step.tool)
        if tool is None:
            raise ToolError(f"Step {number}: there is no tool named '{step.tool}'.", "invalid_steps")
        try:
            args = tool.args_model.model_validate(step.arguments)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "arguments"
            raise ToolError(f"Step {number} ({step.tool}): {where}: {first['msg']}.", "invalid_steps") from None
        step.arguments = args.model_dump(exclude_defaults=True)
        try:
            level = tool.level_for(args, ctx)
        except ToolError:
            level = tool.level
        if level >= RiskLevel.CRITICAL:
            raise ToolError(f"Step {number} ({step.tool}) is blocked by your permission settings.", "blocked")
        highest = max(highest, level)
    return highest


def check_name(name: str) -> str:
    name = " ".join(name.split())
    if not _NAME.match(name):
        raise ToolError("Give the automation a short name (letters, digits, spaces).", "invalid_name")
    return name


class AutomationStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    @staticmethod
    def _row(row: Any) -> AutomationTask:
        return AutomationTask(
            id=row["id"], name=row["name"], enabled=bool(row["enabled"]),
            trigger=_TRIGGER.validate_python(json.loads(row["trigger_config"])),
            steps=[Step.model_validate(s) for s in json.loads(row["actions"])],
            last_run_at=row["last_run_at"], next_run_at=row["next_run_at"], last_status=row["last_status"],
        )

    def save(self, name: str, trigger: Any, steps: list[Step], next_run: datetime | None) -> tuple[AutomationTask, bool]:
        """Create, or replace the one with the same name. Returns (task, replaced)."""
        now = utc_now_iso()
        config = json.dumps(trigger.model_dump(mode="json"))
        actions = json.dumps([s.model_dump(mode="json", exclude_defaults=True) | {"type": s.type} for s in steps])
        next_iso = next_run.isoformat() if next_run else None
        with self._db.transaction() as conn:
            existing = conn.execute("SELECT id FROM automation_tasks WHERE lower(name) = lower(?)", (name,)).fetchone()
            if existing:
                self._keep_revision(conn, existing["id"], "changed")
                conn.execute(
                    "UPDATE automation_tasks SET name=?, enabled=1, trigger_type=?, trigger_config=?, actions=?, "
                    "next_run_at=?, last_status=NULL, updated_at=? WHERE id=?",
                    (name, trigger.type, config, actions, next_iso, now, existing["id"]),
                )
                task_id, replaced = existing["id"], True
            else:
                task_id, replaced = uuid.uuid4().hex, False
                conn.execute(
                    "INSERT INTO automation_tasks (id, name, enabled, trigger_type, trigger_config, actions, next_run_at, "
                    "created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
                    (task_id, name, trigger.type, config, actions, next_iso, now, now),
                )
        return self.get(task_id), replaced  # type: ignore[return-value]

    def get(self, task_id: str) -> AutomationTask | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM automation_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row(row) if row else None

    def find(self, name: str) -> AutomationTask:
        wanted = " ".join(name.lower().split())
        tasks = self.all()
        exact = [t for t in tasks if t.name.lower() == wanted]
        if exact:
            return exact[0]
        partial = [t for t in tasks if wanted and wanted in t.name.lower()]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise ToolError(f"There's no automation called '{name}'.", "not_found")
        raise ToolError("Several automations match: " + ", ".join(t.name for t in partial[:5]) + ". Which one?", "ambiguous")

    def all(self) -> list[AutomationTask]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM automation_tasks ORDER BY name").fetchall()
        tasks = []
        for row in rows:
            try:
                tasks.append(self._row(row))
            except (ValidationError, ValueError, json.JSONDecodeError):
                continue  # a damaged row never breaks the scheduler
        return tasks

    def set_enabled(self, task_id: str, enabled: bool, next_run: datetime | None) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE automation_tasks SET enabled=?, next_run_at=?, updated_at=? WHERE id=?",
                         (int(enabled), next_run.isoformat() if next_run else None, utc_now_iso(), task_id))

    def record_run(self, task_id: str, status: str, next_run: datetime | None, *, disable: bool = False) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE automation_tasks SET last_run_at=?, last_status=?, next_run_at=?, enabled=CASE WHEN ? THEN 0 "
                "ELSE enabled END, updated_at=? WHERE id=?",
                (utc_now_iso(), status[:300], next_run.isoformat() if next_run else None, int(disable), utc_now_iso(), task_id),
            )

    # -- workflow progress (checkpoints): the step number a workflow stopped at

    def progress(self, task_id: str) -> int | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT next_step FROM workflow_progress WHERE task_id = ?", (task_id,)).fetchone()
        return int(row[0]) if row else None

    def progress_outcome(self, task_id: str) -> str | None:
        """'failed' when the checkpointed step is known to have done nothing; None when its outcome is unknown."""
        with self._db.connect() as conn:
            row = conn.execute("SELECT outcome FROM workflow_progress WHERE task_id = ?", (task_id,)).fetchone()
        return row[0] if row else None

    def set_progress(self, task_id: str, next_step: int, outcome: str | None = None) -> None:
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO workflow_progress (task_id, next_step, updated_at, outcome) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(task_id) DO UPDATE SET next_step = excluded.next_step, "
                         "updated_at = excluded.updated_at, outcome = excluded.outcome",
                         (task_id, next_step, utc_now_iso(), outcome))

    def clear_progress(self, task_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM workflow_progress WHERE task_id = ?", (task_id,))

    def delete(self, task_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM workflow_progress WHERE task_id = ?", (task_id,))
            self._keep_revision(conn, task_id, "deleted")
            conn.execute("DELETE FROM automation_tasks WHERE id = ?", (task_id,))

    MAX_REVISIONS = 20

    @classmethod
    def _keep_revision(cls, conn: Any, task_id: str, reason: str) -> None:
        """The version being replaced or deleted, so it can be looked at and restored."""
        row = conn.execute("SELECT name, trigger_type, trigger_config, actions FROM automation_tasks WHERE id = ?",
                           (task_id,)).fetchone()
        if row is None:
            return
        last = conn.execute("SELECT MAX(version) FROM automation_revisions WHERE lower(name) = lower(?)",
                            (row["name"],)).fetchone()[0] or 0
        conn.execute("INSERT INTO automation_revisions (task_id, name, version, saved_at, reason, trigger_type, "
                     "trigger_config, actions) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (task_id, row["name"], last + 1, utc_now_iso(), reason, row["trigger_type"],
                      row["trigger_config"], row["actions"]))
        conn.execute("DELETE FROM automation_revisions WHERE lower(name) = lower(?) AND version <= ?",
                     (row["name"], last + 1 - cls.MAX_REVISIONS))

    def revisions(self, name: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT version, saved_at, reason, trigger_config, actions FROM automation_revisions "
                                "WHERE lower(name) = lower(?) ORDER BY version DESC", (name.strip(),)).fetchall()
        return [{"version": r["version"], "saved_at": r["saved_at"], "reason": r["reason"],
                 "trigger": _TRIGGER.validate_python(json.loads(r["trigger_config"])),
                 "steps": [Step.model_validate(x) for x in json.loads(r["actions"])]} for r in rows]
