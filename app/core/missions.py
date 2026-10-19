"""Missions: multi-step tasks with a plan, checkpoints and recovery, that survive a restart.

"Prepare everything for tomorrow's presentation" -> the AI proposes a plan: a list of steps, each one an
ordinary tool call (open the project folder, find the slides, set a reminder...). Jarvis - not the AI -
then:

1. **validates** the plan: every step names a real tool with valid arguments; tools that change
   automations, memory-wide state or Jarvis itself can't be steps; at most MAX_STEPS steps;
2. **simulates** it (dry run): for each step its risk level, whether it will ask, whether it can be undone
   - "Simulation: 4 steps; 1 asks first (move report.pdf); 3 can be undone";
3. asks you to approve the plan when any step is more than read-only. Approving the plan approves exactly
   those steps with exactly those arguments (MODERATE ones then run without asking again); DANGEROUS steps
   still ask one by one, CRITICAL ones stay blocked;
4. **runs** the steps in order through the executor (validation, permissions, audit, verification), saving
   each step's state as it goes (a checkpoint);
5. **recovers**: a failed read-only step is retried once; anything else pauses the mission and says why -
   "retry the mission", "skip that step", or "cancel the mission". Nothing is repeated blindly.

A mission cut off by a restart is marked paused ("interrupted") and continues on "continue my mission".
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app.ai.base import ToolCall
from app.core import taskstate
from app.security.permissions import RiskLevel
from app.tools.base import ToolError
from app.utils.logging_setup import get_logger, log_event

if TYPE_CHECKING:
    from app.memory.database import Database

_log = get_logger("core.missions")
MAX_STEPS = 12
NOT_A_STEP = frozenset({
    "start_mission", "resume_mission", "cancel_mission", "mission_status", "mission_history", "rollback_mission", "task_center", "undo_last_action", "set_reminder",
    "schedule_task", "save_workflow", "teach_workflow", "run_workflow", "manage_automation", "forget_everything", "forget_history",
    "change_setting", "jarvis_control", "power_control", "windows_update", "close_all_apps", "end_process",
    "propose_code_change", "test_proposal", "apply_proposal", "rollback_proposal", "restore_workflow",
    "delegate", "run_team",  # never in a mission (orchestration doesn't nest)
})
STATES = taskstate.MISSION_STATES


@dataclass
class Step:
    n: int
    title: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    state: str = "pending"  # pending | running | done | failed | skipped
    attempts: int = 0
    result: str = ""
    level: int = 0  # filled in by the simulation
    reversible: bool = False
    depends_on: list[int] = field(default_factory=list)  # earlier steps that must be done first
    milestone: str = ""  # the milestone this step belongs to (for progress by milestone)
    checkpoint: bool = False  # pause for your go-ahead before this step
    inverse: dict | None = None  # how to undo it once done ({"tool", "arguments", "description"})


@dataclass
class Mission:
    id: str
    title: str
    state: str
    steps: list[Step]
    goal_id: int | None = None
    note: str = ""
    updated_at: str = ""

    @property
    def next_step(self) -> Step | None:
        return next((s for s in self.steps if s.state in ("pending", "failed", "running")), None)

    def progress(self) -> str:
        done = sum(1 for s in self.steps if s.state in ("done", "skipped"))
        return f"{done}/{len(self.steps)}"

    def milestones(self) -> list[tuple[str, int, int]]:
        """[(milestone, done, total)] in plan order (steps without one are left out)."""
        order: dict[str, list[int]] = {}
        for step in self.steps:
            if step.milestone:
                counts = order.setdefault(step.milestone, [0, 0])
                counts[1] += 1
                counts[0] += step.state in ("done", "skipped")
        return [(name, done, total) for name, (done, total) in order.items()]


# ------------------------------------------------------------------ plan: validate and simulate


def validate_plan(raw_steps: list[dict], registry, context) -> list[Step]:
    if not raw_steps:
        raise ToolError("A mission needs at least one step.", "invalid_plan")
    if len(raw_steps) > MAX_STEPS:
        raise ToolError(f"A mission can have at most {MAX_STEPS} steps; split it up.", "invalid_plan")
    steps: list[Step] = []
    for n, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            raise ToolError(f"Step {n} is not a step.", "invalid_plan")
        name = str(item.get("tool", ""))
        if name in NOT_A_STEP:
            raise ToolError(f"Step {n}: '{name}' can't be part of a mission.", "invalid_plan")
        tool = registry.get(name)
        if tool is None:
            raise ToolError(f"Step {n}: there is no tool named '{name}'.", "invalid_plan")
        arguments = item.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ToolError(f"Step {n}: arguments must be an object.", "invalid_plan")
        try:
            args = tool.args_model.model_validate(arguments)
        except ValidationError as exc:
            issue = exc.errors()[0]
            raise ToolError(f"Step {n} ({name}): {'.'.join(map(str, issue['loc'])) or 'arguments'}: {issue['msg']}.",
                            "invalid_plan") from None
        title = " ".join(str(item.get("title") or name.replace("_", " ")).split())[:120]
        depends = item.get("depends_on") or []
        if not isinstance(depends, list) or not all(isinstance(d, int) for d in depends):
            raise ToolError(f"Step {n}: depends_on must be a list of step numbers.", "invalid_plan")
        if any(d >= n or d < 1 for d in depends):
            raise ToolError(f"Step {n} can only depend on earlier steps.", "invalid_plan")
        steps.append(Step(n, title, name, args.model_dump(exclude_defaults=True), depends_on=sorted(set(depends)),
                          milestone=" ".join(str(item.get("milestone") or "").split())[:80],
                          checkpoint=bool(item.get("checkpoint"))))
    return steps


def simulate(steps: list[Step], registry, permissions, context) -> tuple[RiskLevel, str]:
    """Fill in each step's level and reversibility; the highest level and a plain-language summary."""
    from app.core.undo import inverse_of
    from app.tools.base import ToolResult

    highest = RiskLevel.SAFE
    asks, blocked, reversible = [], [], 0
    for step in steps:
        tool = registry.get(step.tool)
        try:
            args = tool.args_model.model_validate(step.arguments)
            _, level = permissions.decide(tool.name, tool.level_for(args, context))
        except Exception:
            level = RiskLevel.DANGEROUS  # can't evaluate it now: treat it as needing you
        step.level = int(level)
        highest = RiskLevel(max(highest, level))
        probe = ToolResult.success("", path="x", old_path="y", previous=0)
        step.reversible = inverse_of(ToolCall(step.tool, step.arguments), probe) is not None
        reversible += step.reversible
        if level == RiskLevel.CRITICAL:
            blocked.append(f"{step.n}. {step.title}")
        elif level >= RiskLevel.DANGEROUS:
            asks.append(f"{step.n}. {step.title}")
    lines = [f"{s.n}. {s.title}" + (" (asks you)" if s.level >= 2 else "") + (" (undoable)" if s.reversible else "")
             for s in steps]
    summary = f"{len(steps)} step(s): " + "; ".join(lines) + "."
    if asks:
        summary += f" These still ask one by one: {', '.join(asks)}."
    if blocked:
        summary += f" Blocked by your settings: {', '.join(blocked)}."
    return highest, summary


# ------------------------------------------------------------------ storage


class MissionStore:
    def __init__(self, database: "Database") -> None:
        self._db = database

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def create(self, title: str, steps: list[Step], goal_id: int | None = None, run_id: str | None = None) -> Mission:
        mission = Mission(time.strftime("m%Y%m%d-%H%M%S-") + secrets.token_hex(2), title[:200], "planned", steps,
                          goal_id)
        now = self._now()
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO missions (id, title, goal_id, state, run_id, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (mission.id, mission.title, goal_id, "planned", run_id, now, now))
            for step in steps:
                conn.execute("INSERT INTO mission_steps (mission_id, n, title, tool, arguments, depends_on, milestone, "
                             "checkpoint) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (mission.id, step.n, step.title, step.tool, json.dumps(step.arguments, ensure_ascii=False),
                              json.dumps(step.depends_on), step.milestone, int(step.checkpoint)))
        return mission

    def get(self, mission_id: str) -> Mission:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
            if row is None:
                raise ToolError("There's no such mission.", "not_found")
            steps = []
            for r in conn.execute("SELECT * FROM mission_steps WHERE mission_id = ? ORDER BY n", (mission_id,)):
                keys = r.keys()
                steps.append(Step(r["n"], r["title"], r["tool"], json.loads(r["arguments"]), r["state"], r["attempts"],
                                  r["result"], depends_on=json.loads(r["depends_on"] or "[]"),
                                  milestone=r["milestone"] if "milestone" in keys else "",
                                  checkpoint=bool(r["checkpoint"]) if "checkpoint" in keys else False,
                                  inverse=json.loads(r["inverse"]) if "inverse" in keys and r["inverse"] else None))
        return Mission(row["id"], row["title"], row["state"], steps, row["goal_id"], row["note"],
                       row["updated_at"] or "")

    def save_step(self, mission: Mission, step: Step) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE mission_steps SET state = ?, attempts = ?, result = ?, inverse = ? WHERE mission_id = ? "
                         "AND n = ?", (step.state, step.attempts, step.result[:500],
                                       json.dumps(step.inverse) if step.inverse else "", mission.id, step.n))
            conn.execute("UPDATE missions SET updated_at = ? WHERE id = ?", (self._now(), mission.id))

    def set_state(self, mission: Mission, state: str, note: str = "") -> None:
        """Move the mission to ``state`` if the lifecycle allows it (see app/core/taskstate.py) and record why."""
        from app.core import runs

        old = mission.state
        try:
            taskstate.check_transition(old, state)
        except taskstate.InvalidTransition as exc:
            log_event(_log, "mission_invalid_transition", mission=mission.id, old=old, new=state)
            raise ToolError(f"Mission '{mission.title}': {exc}.", "invalid_transition") from None
        mission.state, mission.note = state, note
        run = runs.current()
        with self._db.transaction() as conn:
            conn.execute("UPDATE missions SET state = ?, note = ?, updated_at = ? WHERE id = ?",
                         (state, note[:500], self._now(), mission.id))
            if old != state:
                conn.execute("INSERT INTO task_events (task_id, kind, ts, old_state, new_state, note, run_id) "
                             "VALUES (?, 'mission', ?, ?, ?, ?, ?)",
                             (mission.id, self._now(), old, state, note[:300], run.id if run else ""))

    def history(self, mission_id: str) -> list[dict]:
        """The mission's state changes, oldest first: what happened and why."""
        with self._db.connect() as conn:
            rows = conn.execute("SELECT ts, old_state, new_state, note, run_id FROM task_events WHERE task_id = ? "
                                "ORDER BY id", (mission_id,)).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 10, states: tuple[str, ...] | None = None) -> list[Mission]:
        query, params = "SELECT id FROM missions", []
        if states:
            query += f" WHERE state IN ({','.join('?' * len(states))})"
            params = list(states)
        with self._db.connect() as conn:
            ids = [r[0] for r in conn.execute(query + " ORDER BY updated_at DESC, id DESC LIMIT ?", (*params, limit))]
        return [self.get(i) for i in ids]

    def unfinished(self) -> Mission | None:
        found = self.recent(1, ("paused", "running", "verifying", "planned"))
        return found[0] if found else None

    def mark_interrupted(self) -> int:
        with self._db.transaction() as conn:
            return conn.execute("UPDATE missions SET state = 'paused', note = 'interrupted (Jarvis restarted)' "
                                "WHERE state IN ('running', 'verifying')").rowcount


# ------------------------------------------------------------------ live control (pause / cancel a running mission)

_REQUESTS: dict[str, str] = {}
_REQUESTS_LOCK = threading.Lock()


def request(mission_id: str, kind: str) -> None:
    """Ask a RUNNING mission to "pause" or "cancel". It stops at the next step boundary (a step that already
    started finishes or times out; nothing is cut off half-way), then the mission is paused / cancelled."""
    if kind not in ("pause", "cancel"):
        raise ValueError(kind)
    with _REQUESTS_LOCK:
        _REQUESTS[mission_id] = kind


def _take_request(mission_id: str) -> str | None:
    with _REQUESTS_LOCK:
        return _REQUESTS.pop(mission_id, None)


def _honour_request(mission: "Mission", store: "MissionStore", where: str) -> "Outcome | None":
    kind = _take_request(mission.id)
    if kind is None:
        return None
    if kind == "cancel":
        store.set_state(mission, "cancelled", f"cancelled by you {where}")
        _publish("MissionCancelled", title=mission.title)
        return Outcome(mission, f"Cancelled '{mission.title}' {where}. Finished steps stay done "
                                "('undo the mission' rolls them back where possible).", False)
    note = f"paused by you {where}"
    store.set_state(mission, "paused", note)
    _publish("MissionPaused", title=mission.title, note=note)
    return Outcome(mission, f"Paused '{mission.title}' {where}. Say 'continue my mission' to go on.", False)


# ------------------------------------------------------------------ running


def _publish(name: str, **data) -> None:
    from app.core import events

    try:
        events.BUS.publish(name, **data)
    except Exception:
        _log.warning("could not publish %s", name, exc_info=True)


@dataclass
class Outcome:
    mission: Mission
    message: str
    ok: bool


async def run(mission: Mission, store: MissionStore, executor, *, approved: bool, skip_failed: bool = False,
              on_step=None) -> Outcome:
    """Run from the first unfinished step. ``approved``: you approved this exact plan."""
    if mission.state in ("completed", "cancelled", "rolled_back"):
        return Outcome(mission, f"'{mission.title}' is already {mission.state.replace('_', ' ')}.",
                       mission.state == "completed")
    if skip_failed:
        for step in mission.steps:
            if step.state == "failed":
                step.state, step.result = "skipped", step.result or "skipped by you"
                store.save_step(mission, step)
                break
    store.set_state(mission, "running")
    # A step still marked "running" was cut off (restart, crash, stop): we can't know whether it took effect.
    # Read-only steps are simply run again; anything else is never repeated blindly - you decide.
    for step in mission.steps:
        if step.state != "running":
            continue
        tool = executor.registry.get(step.tool)
        if tool is not None and tool.level == RiskLevel.SAFE and tool.verify is None:
            continue
        step.state = "failed"
        step.result = "interrupted: it may or may not have completed before Jarvis stopped"
        store.save_step(mission, step)
        note = (f"Paused at step {step.n} ({step.title}): it was interrupted and may already have happened. "
                "Check, then say 'skip that step' if it did, 'retry the mission' to run it again, or 'cancel the mission'.")
        store.set_state(mission, "paused", note)
        _publish("MissionPaused", title=mission.title, note=note)
        return Outcome(mission, note, False)
    by_n = {s.n: s for s in mission.steps}
    try:
        for step in mission.steps:
            if step.state in ("done", "skipped", "undone"):
                continue
            stopped = _honour_request(mission, store, f"before step {step.n} ({step.title})")
            if stopped is not None:
                return stopped
            from app.core import emergency

            if emergency.STATE.flags.get("automation_disabled"):  # the global pause: stop at the next step boundary
                note = f"Paused before step {step.n} ({step.title}): automations are switched off (emergency controls)"
                store.set_state(mission, "paused", note)
                _publish("MissionPaused", title=mission.title, note=note)
                return Outcome(mission, note + ". Turn them back on in the window, then say 'continue my mission'.",
                               False)
            blocked = [d for d in step.depends_on if by_n.get(d) is not None and by_n[d].state != "done"]
            if blocked:  # a step it needs was skipped (a failed one would have paused the mission)
                step.state, step.result = "skipped", f"skipped: needs step {blocked[0]}, which didn't run"
                store.save_step(mission, step)
                continue
            if step.checkpoint and step.state != "approved":
                step.state = "waiting"
                store.save_step(mission, step)
                note = f"Checkpoint before step {step.n} ({step.title}): waiting for your go-ahead"
                store.set_state(mission, "paused", note)
                _publish("MissionPaused", title=mission.title, note=note)
                return Outcome(mission, note + ". Say 'continue my mission' to go on, or 'cancel the mission'.", False)
            if on_step is not None:
                on_step(mission, step)
            while True:
                step.state, step.attempts = "running", step.attempts + 1
                store.save_step(mission, step)
                call = ToolCall(step.tool, dict(step.arguments))
                execution = await executor.execute(call, preapproved=approved)
                result = execution.result
                step.result = result.message or (result.error or "")
                if result.ok:
                    step.state = "done"
                    from app.core.undo import inverse_of

                    inverse = inverse_of(call, result)
                    step.inverse = {"tool": inverse.call.name, "arguments": dict(inverse.call.arguments),
                                    "description": inverse.description} if inverse is not None else None
                    store.save_step(mission, step)
                    break
                level = int(execution.level) if execution.level is not None else 3
                from app.core import retry

                again, wait, why = retry.should_retry(result.error, result.message or "", step.attempts,
                                                      repeatable=(level == 0))
                if again and _REQUESTS.get(mission.id) is None:      # a pause / cancel request wins over a retry
                    log_event(_log, "mission_step_retry", mission=mission.id, step=step.n, error=result.error,
                              why=why)
                    await asyncio.sleep(wait)
                    continue
                step.result = f"{step.result} ({why})" if step.result else why
                step.state = "failed"
                store.save_step(mission, step)
                note = f"Paused at step {step.n} ({step.title}): {result.message}"
                store.set_state(mission, "paused", note)
                _publish("MissionPaused", title=mission.title, note=note)
                return Outcome(mission, note + " Say 'retry the mission', 'skip that step' or 'cancel the mission'.",
                               False)
    except asyncio.CancelledError:
        store.set_state(mission, "paused", "stopped by you")
        raise
    store.set_state(mission, "verifying")
    ok, note = taskstate.verify_completion(mission.steps)
    if not ok:
        store.set_state(mission, "paused", f"not complete: {note}")
        _publish("MissionPaused", title=mission.title, note=note)
        return Outcome(mission, f"'{mission.title}' is not complete: {note}.", False)
    store.set_state(mission, "completed", note)
    _publish("MissionCompleted", title=mission.title, steps=len(mission.steps))
    log_event(_log, "mission_completed", mission=mission.id, steps=len(mission.steps))
    done = sum(1 for s in mission.steps if s.state == "done")
    return Outcome(mission, f"Mission '{mission.title}' completed: {done} step(s) done" +
                   (f" ({note})" if note else "") + ".", True)


def approve_checkpoint(mission: Mission, store: MissionStore) -> Step | None:
    """'continue my mission' at a checkpoint: the waiting step may now run."""
    for step in mission.steps:
        if step.state == "waiting":
            step.state = "approved"
            store.save_step(mission, step)
            return step
    return None


async def rollback(mission: Mission, store: MissionStore, executor) -> Outcome:
    """Undo the finished steps, newest first, each through the executor (its own checks and audit). Steps that
    can't be undone are listed, never pretended."""
    from app.core import undo

    undone, kept, failed = [], [], []
    token = undo.UNDOING.set(True)
    try:
        for step in reversed(mission.steps):
            if step.state != "done":
                continue
            if not step.inverse:
                kept.append(f"{step.n} ({step.title})")
                continue
            execution = await executor.execute(ToolCall(step.inverse["tool"], dict(step.inverse["arguments"])))
            if execution.result.ok:
                step.state, step.result = "undone", f"undone: {step.inverse.get('description', '')}"
                store.save_step(mission, step)
                undone.append(step.n)
            else:
                failed.append(f"{step.n}: {execution.result.message}")
    finally:
        undo.UNDOING.reset(token)
    store.set_state(mission, "rolled_back" if not failed else "paused",
                    "rolled back" if not failed else "rollback incomplete")
    parts = [f"Rolled back '{mission.title}': {len(undone)} step(s) undone"]
    if kept:
        parts.append("these can't be undone and stay as they are: " + ", ".join(kept))
    if failed:
        parts.append("undoing failed for " + "; ".join(failed))
    log_event(_log, "mission_rolled_back", mission=mission.id, undone=len(undone), kept=len(kept), failed=len(failed))
    return Outcome(mission, ". ".join(parts) + ".", not failed)
