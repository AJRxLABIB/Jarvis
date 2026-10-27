"""The time-travel task debugger: rebuild what happened in a run, find where it first went wrong, and re-examine it safely.

It reads only what Jarvis already records (``agent_runs`` + ``audit_log`` of one run: redacted arguments, risk, decisions,
confirmations, verification, results, durations). Nothing new is captured - no screenshots, audio, keystrokes or file contents.

Three replay modes, each honest about what it is:

* **trace**      - the recorded sequence, every line tagged ``[recorded]`` (a stored fact), ``[reconstructed]`` (worked out
                   here, e.g. grouping events into calls or deciding a call is repeatable) or ``[not recorded]``.
* **simulate**   - the REAL mission engine (``app/core/missions.py``) is run against a MOCK executor that answers with the
                   recorded results, in a throw-away database. Nothing external runs. It shows whether the engine's retry /
                   pause logic reproduces what happened. It says nothing about whether the world would behave the same now.
* **live**       - one recorded call is run again through the real executor (so permissions, emergency stop and argument
                   validation are re-checked NOW) - but only if the action is read-only/repeatable and its recorded arguments
                   are intact. Anything that changes something (a deletion, a message, a purchase, a device command) is never
                   replayed, however the earlier attempt ended.

Determinism is not claimed: model output, external state and timing are not reproduced. Missing data is named, never invented.
Sensitive-tool arguments and results are hidden (default list, which you can extend; removing one is a deliberate act).
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core import capability_gaps, retry
from app.memory.settings_store import SettingsStore
from app.security import autonomy
from app.utils.redaction import REDACTED, redact_value

EXCLUDE_KEY = "replay.exclude_tools"
DEFAULT_EXCLUDED = frozenset({"type_text", "send_email", "send_message", "read_file", "read_document", "read_whatsapp",
                              "clipboard", "clipboard_action", "run_command", "remember", "journal", "save_contact"})
HIDDEN = "[hidden by your replay settings]"
MESSAGE_LIMIT = 200
RECORDED, RECONSTRUCTED, MISSING = "recorded", "reconstructed", "not recorded"


# ------------------------------------------------------------------ settings


def excluded(db: Any) -> set[str]:
    extra = SettingsStore(db).get(EXCLUDE_KEY, None)
    if isinstance(extra, dict):
        return (set(DEFAULT_EXCLUDED) | {str(t) for t in extra.get("add", [])}) - {str(t) for t in extra.get("remove", [])}
    return set(DEFAULT_EXCLUDED)


def set_excluded(db: Any, tool: str, hide: bool) -> set[str]:
    store = SettingsStore(db)
    cfg = store.get(EXCLUDE_KEY, None)
    cfg = cfg if isinstance(cfg, dict) else {"add": [], "remove": []}
    add, remove = set(cfg.get("add", [])), set(cfg.get("remove", []))
    (add.add(tool), remove.discard(tool)) if hide else (remove.add(tool), add.discard(tool))
    store.set(EXCLUDE_KEY, {"add": sorted(add), "remove": sorted(remove)})
    return excluded(db)


# ------------------------------------------------------------------ the timeline


@dataclass
class Call:
    n: int
    tool: str
    arguments: dict[str, Any] | None = None       # None = hidden / not recorded
    args_state: str = RECORDED                     # recorded | hidden | not recorded | redacted
    level: int | None = None
    started: str = ""
    ended: str = ""
    elapsed_ms: int | None = None
    ok: bool | None = None                         # None = no result was recorded
    error: str | None = None
    message: str = ""
    asked: bool = False
    answer: str | None = None
    refused_by: str = ""
    verified: bool | None = None
    verify_note: str = ""
    preapproved: bool = False
    direct: bool = False
    missing: list[str] = field(default_factory=list)

    @property
    def repeatable(self) -> bool | None:
        """Reconstructed, never recorded: may this call be run again without risk of doing something twice?"""
        if self.arguments is None or self.args_state != RECORDED:
            return False
        if self.level is not None and self.level > 0:
            return False
        return autonomy.is_read_only(self.tool, self.arguments)


@dataclass
class Divergence:
    call: int
    tool: str
    kind: str              # refused | failed | unverified | no_outcome
    expected: str
    actual: str
    causes: list[str]
    recovery: list[str]


@dataclass
class Timeline:
    run: dict[str, Any]
    calls: list[Call]
    gaps: list[str] = field(default_factory=list)
    divergence: Divergence | None = None

    @property
    def run_id(self) -> str:
        return str(self.run.get("id", ""))


def _details(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(entry.get("details") or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def build(db: Any, run_id: str) -> Timeline:
    from app.core.runs import RunStore

    store = RunStore(db)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        from app.tools.base import ToolError

        raise ToolError("There's no recorded run with that id.", "not_found")
    run = dict(row)
    hidden = excluded(db)
    calls: list[Call] = []
    open_calls: dict[str, Call] = {}
    for entry in store.trace(run_id):
        tool = entry.get("tool") or ""
        if not tool:
            continue
        call = open_calls.get(tool)
        details = _details(entry)
        if call is None:
            call = Call(len(calls) + 1, tool, started=str(entry.get("ts") or ""))
            open_calls[tool] = call
            calls.append(call)
        if entry.get("permission_level") is not None:
            call.level = int(entry["permission_level"])
        event = entry.get("event")
        args = details.get("arguments")
        if isinstance(args, dict) and call.arguments is None and call.args_state == RECORDED:
            if tool in hidden:
                call.args_state = "hidden"
            else:
                call.arguments = redact_value(args)
                if REDACTED in json.dumps(call.arguments, default=str):
                    call.args_state = "redacted"
        if event == "confirmation_requested":
            call.asked = True
        elif event == "confirmation_answered":
            call.answer = entry.get("decision")
        elif event == "confirmation_preapproved":
            call.preapproved = True
        elif event == "confirmation_skipped":
            call.direct = True
        elif event == "agent_contract_denied":
            call.refused_by = str(details.get("reason", ""))
        elif event == "verification":
            call.verified, call.verify_note = bool(details.get("ok")), str(details.get("found", ""))[:MESSAGE_LIMIT]
        elif event == "tool_result":
            call.ok, call.error = bool(details.get("ok")), details.get("error")
            call.message = HIDDEN if tool in hidden else str(redact_value(str(details.get("message") or "")))[:MESSAGE_LIMIT]
            call.ended = str(entry.get("ts") or "")
            ms = details.get("elapsed_ms")
            call.elapsed_ms = int(ms) if isinstance(ms, (int, float)) else None
            open_calls.pop(tool, None)
    gaps: list[str] = []
    for c in calls:
        if c.arguments is None and c.args_state == RECORDED:
            c.args_state = MISSING
            c.missing.append("arguments were not recorded")
        if c.args_state == "hidden":
            c.missing.append("arguments hidden by your settings")
        if c.ok is None and not c.refused_by:
            c.missing.append("no result was recorded (the run may have been interrupted)")
        elif c.ok is not None and c.elapsed_ms is None:
            c.missing.append("duration not recorded (older record)")
    if not calls:
        gaps.append("No tool calls were recorded for this run - it may have been answered without actions, or its audit "
                    "records were deleted.")
    if run.get("state") in ("failed", "interrupted") and calls and all(c.ok is not False and not c.refused_by for c in calls):
        gaps.append(f"The run ended '{run.get('state')}' but no failing call was recorded; the cause is not in the audit trail.")
    timeline = Timeline(run, calls, gaps)
    timeline.divergence = find_divergence(timeline)
    return timeline


# ------------------------------------------------------------------ divergence and recovery


def find_divergence(timeline: Timeline) -> Divergence | None:
    """The EARLIEST call that did not do what it was expected to: ran, and was confirmed where a check exists."""
    for c in timeline.calls:
        if c.refused_by:
            return _divergence(c, "refused", "the call is allowed to run", f"it was refused: {c.refused_by}")
        if c.ok is None:
            return _divergence(c, "no_outcome", "a recorded result", "no result was recorded")
        if c.ok is False:
            kind = "unverified" if c.error == "not_verified" else "failed"
            return _divergence(c, kind, "the call succeeds and its effect is confirmed",
                               f"it failed ({c.error or 'no reason recorded'}): {c.message}".strip())
        if c.verified is False:
            return _divergence(c, "unverified", "the effect is confirmed afterwards",
                               f"the check afterwards said: {c.verify_note or 'not confirmed'}")
    return None


def _divergence(c: Call, kind: str, expected: str, actual: str) -> Divergence:
    code = c.error or ("refused" if kind == "refused" else "")
    cause_key = capability_gaps.classify(code) if code else "unclassified"
    causes = [capability_gaps.CAUSES.get(cause_key, "the cause isn't clear from the record")]
    if kind == "no_outcome":
        causes = ["the run stopped before the result was written (a crash, restart or emergency stop)"]
    return Divergence(c.n, c.tool, kind, expected, actual, causes, recovery_plan(c, kind))


def recovery_plan(c: Call, kind: str) -> list[str]:
    """The smallest sensible next steps. Repeating a call is only ever suggested when it is provably repeatable."""
    policy = retry.classify(c.error, c.message)
    repeatable = bool(c.repeatable)
    if kind == "refused" or policy == "permission":
        return ["Do not widen any permission to get past this.", "Decide whether this action should be allowed; if so, ask again "
                "and approve it on screen."]
    if kind == "no_outcome":
        return [f"Check the real state first: did '{c.tool}' take effect before the run stopped?",
                "Only if it did not, run it again" + (" (it is read-only, so `replay live` can do that)." if repeatable
                                                      else " yourself - it changes things, so Jarvis will not repeat it automatically.")]
    if policy == "validation":
        return ["The request or its arguments were rejected; repeating the same call will fail the same way.",
                "Correct the missing or invalid detail (see the message), then ask again."]
    if kind == "unverified" or policy == "verification":
        return [f"Look at the real state - '{c.tool}' reported work the check did not confirm.",
                "Re-run only if the check shows it did not happen" + ("." if repeatable else
                                                                      "; it changes things, so confirm manually.")]
    if policy in ("transient", "resource"):
        if repeatable:
            return ["The cause looks temporary (offline, busy or slow). Fix or wait it out, then `replay live` re-runs this "
                    "read-only call once to check."]
        return ["The cause looks temporary, but this action changes things and may already have happened.",
                "Check the real state, then repeat it yourself if needed - it is never repeated automatically."]
    return ["The record doesn't show enough to name a cause. Run `investigate` / `capability_report`, or collect the next "
            "occurrence, before changing anything."]


# ------------------------------------------------------------------ rendering: trace replay


def _tag(kind: str, text: str) -> str:
    return f"[{kind}] {text}"


def trace_lines(t: Timeline) -> list[str]:
    run = t.run
    head = f"Run {t.run_id}: {run.get('state')} ({run.get('source')}), started {str(run.get('started_at', ''))[:19]}"
    lines = [_tag(RECORDED, head), _tag(RECORDED, f"request: {run.get('text') or '(none stored)'}")]
    for c in t.calls:
        lines.append(_tag(RECORDED, f"{c.n}. {c.tool} at {c.started[11:19] or '?'}"
                          + (f", risk {c.level}" if c.level is not None else "")))
        if c.arguments is not None:
            lines.append("     " + _tag(RECORDED, "arguments: " + json.dumps(c.arguments, ensure_ascii=False, default=str)[:300]))
        elif c.args_state == "hidden":
            lines.append("     " + _tag(MISSING, "arguments: " + HIDDEN))
        else:
            lines.append("     " + _tag(MISSING, "arguments were not recorded"))
        if c.asked:
            lines.append("     " + _tag(RECORDED, f"you were asked; answer: {c.answer or 'none recorded'}"))
        elif c.preapproved or c.direct:
            lines.append("     " + _tag(RECORDED, "ran without asking (" + ("approved plan" if c.preapproved else "your direct command") + ")"))
        if c.refused_by:
            lines.append("     " + _tag(RECORDED, f"refused: {c.refused_by}"))
        if c.ok is not None:
            took = f" in {c.elapsed_ms} ms" if c.elapsed_ms is not None else ""
            lines.append("     " + _tag(RECORDED, f"{'ok' if c.ok else 'failed' + (' (' + c.error + ')' if c.error else '')}{took}: {c.message}"))
        if c.verified is not None:
            lines.append("     " + _tag(RECORDED, f"checked afterwards: {'confirmed' if c.verified else 'NOT confirmed - ' + c.verify_note}"))
        for m in c.missing:
            lines.append("     " + _tag(MISSING, m))
        lines.append("     " + _tag(RECONSTRUCTED, "can be run again safely" if c.repeatable else "changes things or can't be verified as "
                                                                                     "repeatable: never replayed automatically"))
    for g in t.gaps:
        lines.append(_tag(MISSING, g))
    d = t.divergence
    if d is not None:
        lines.append(_tag(RECONSTRUCTED, f"earliest divergence: call {d.call} ({d.tool}) - expected {d.expected}; actually {d.actual}"))
    return lines


def diagnose_text(t: Timeline) -> str:
    d = t.divergence
    if d is None:
        extra = " ".join(t.gaps)
        return "No divergence is visible in what was recorded: every recorded call succeeded" + (
            " and was confirmed." if all(c.verified is not False for c in t.calls) else ".") + (f" {extra}" if extra else "")
    after = [c for c in t.calls if c.n > d.call]
    text = [f"First divergence: call {d.call}, {d.tool}. Expected: {d.expected}. Actual: {d.actual}.",
            "Likely cause (from the error code, not proof): " + "; ".join(d.causes) + ".", "Minimal recovery:"]
    text += [f"  - {step}" for step in d.recovery]
    if after:
        text.append(f"{len(after)} later call(s) are recorded after it; their results may be affected by that failure.")
    text.append("Everything above is reconstructed from the audit trail; the model's own reasoning and the state of your "
                "PC at the time were not recorded.")
    return "\n".join(text)


# ------------------------------------------------------------------ simulation replay (mock tools, real mission engine)


@dataclass
class Simulation:
    final_state: str
    paused_at: int | None
    attempts: dict[int, int]
    matches: bool
    notes: list[str]

    def describe(self) -> str:
        where = f" at step {self.paused_at}" if self.paused_at else ""
        retries = [f"step {n} tried {a}x" for n, a in sorted(self.attempts.items()) if a > 1]
        return (f"SIMULATED (mock tools, recorded results; nothing external ran): the mission engine ended '{self.final_state}'{where}"
                + (f"; {', '.join(retries)}" if retries else "") + f". It {'reproduces' if self.matches else 'does NOT reproduce'} "
                "what was recorded. " + " ".join(self.notes))


class _MockExecutor:
    """Answers with recorded results. It has no registry of real tools, so nothing real can run."""

    def __init__(self, queues: dict[tuple[str, str], list[Call]]) -> None:
        self.queues = queues
        self.calls: list[str] = []
        self.registry = SimpleNamespace(get=lambda tool: SimpleNamespace(level=0, verify=None))

    async def execute(self, call: Any, **_: Any):
        from app.agent.tool_manager import ToolExecution
        from app.tools.base import ToolResult

        key = (call.name, json.dumps(call.arguments, sort_keys=True, default=str))
        self.calls.append(call.name)
        queue = self.queues.get(key) or []
        if not queue:
            return ToolExecution(call, ToolResult.failure("no_recording", "the recording has no further result for this call"), 0)
        rec = queue.pop(0)
        result = ToolResult.success(rec.message) if rec.ok else ToolResult.failure(rec.error or "failed", rec.message or "failed")
        return ToolExecution(call, result, 0)


async def simulate(t: Timeline) -> Simulation:
    import asyncio
    from unittest import mock

    from app.core import missions
    from app.memory.database import Database

    usable = [c for c in t.calls if c.ok is not None and c.arguments is not None and c.args_state == RECORDED]
    if not usable:
        return Simulation("not run", None, {}, False, ["No call has both its arguments and its result recorded, so there is "
                                                         "nothing to simulate."])
    # consecutive identical calls were retries: one step, several recorded results (the engine does the retrying)
    steps: list[dict[str, Any]] = []
    queues: dict[tuple[str, str], list[Call]] = {}
    for c in usable:
        key = (c.tool, json.dumps(c.arguments, sort_keys=True, default=str))
        queues.setdefault(key, []).append(c)
        if steps and steps[-1]["key"] == key:
            continue
        steps.append({"key": key, "tool": c.tool, "arguments": c.arguments})
    notes = [f"{len(t.calls) - len(usable)} call(s) lacked arguments or a result and were left out."] if len(usable) != len(t.calls) else []
    sleeps: list[float] = []

    async def no_wait(seconds: float) -> None:      # recorded retries already happened; don't make the simulation wait
        sleeps.append(seconds)

    with tempfile.TemporaryDirectory(prefix="jarvis-replay-") as folder:
        db = Database(Path(folder) / "sim.db")
        db.initialize()
        store = missions.MissionStore(db)
        plan = [missions.Step(i + 1, f"{s['tool']}", s["tool"], dict(s["arguments"])) for i, s in enumerate(steps)]
        mission = store.create("simulation of " + t.run_id, plan)
        executor = _MockExecutor(queues)
        with mock.patch.object(asyncio, "sleep", no_wait):
            outcome = await missions.run(mission, store, executor, approved=True)
    final = mission.state
    paused = next((s.n for s in mission.steps if s.state == "failed"), None)
    recorded_fail = t.divergence.call if t.divergence else None
    recorded_failed_step = None
    if recorded_fail is not None:
        bad = next((c for c in t.calls if c.n == recorded_fail), None)
        if bad is not None:
            key = (bad.tool, json.dumps(bad.arguments, sort_keys=True, default=str))
            recorded_failed_step = next((i + 1 for i, s in enumerate(steps) if s["key"] == key), None)
    matches = (paused == recorded_failed_step) if recorded_fail is not None else (final == "completed")
    if outcome.message and not outcome.ok:
        notes.append(f"Engine said: {outcome.message[:160]}")
    return Simulation(final, paused, {s.n: s.attempts for s in mission.steps}, matches, notes)


# ------------------------------------------------------------------ live reproduction (read-only calls only)


class LiveRefused(Exception):
    pass


def live_check(t: Timeline, number: int) -> Call:
    """Why a call may or may not be replayed live. Raises LiveRefused with the reason."""
    call = next((c for c in t.calls if c.n == number), None)
    if call is None:
        raise LiveRefused(f"There is no call {number} in that run.")
    if call.args_state != RECORDED or call.arguments is None:
        raise LiveRefused("Its arguments were not recorded intact (hidden, redacted or missing), so it can't be run again faithfully.")
    if not call.repeatable:
        raise LiveRefused(f"'{call.tool}' can change things (or isn't known to be read-only), so it is never replayed automatically - "
                          "an earlier failure is not a reason to do it again. Check the real state and repeat it yourself if needed.")
    return call


async def live(ctx: Any, t: Timeline, number: int) -> str:
    """Run one repeatable call again through the real executor (permissions, emergency stop and validation apply now)."""
    from app.ai.base import ToolCall

    call = live_check(t, number)
    executor = getattr(ctx, "executor", None)
    if executor is None:
        raise LiveRefused("Live reproduction needs the assistant's executor.")
    execution = await executor.execute(ToolCall(call.tool, dict(call.arguments or {})))
    now = execution.result
    before = "worked" if call.ok else f"failed ({call.error})"
    if now.ok == bool(call.ok) and (now.ok or now.error == call.error):
        verdict = "The same outcome again - the problem is reproducible." if not now.ok else "It still works - it did not fail this time."
    elif now.ok:
        verdict = "It works now: the earlier failure looks temporary or has been fixed."
    else:
        verdict = f"It now fails differently ({now.error}): the situation has changed."
    return (f"LIVE re-run of call {call.n} ({call.tool}, read-only): earlier it {before}; now it {'worked' if now.ok else 'failed (' + str(now.error) + ')'}: "
            f"{now.message[:160]}. {verdict} (Not a deterministic replay: time, network and application state differ.)")


# ------------------------------------------------------------------ finding runs


def failing_runs(db: Any, limit: int = 30) -> list[tuple[dict[str, Any], Divergence | None]]:
    from app.core.runs import RunStore

    out = []
    for run in RunStore(db).recent(limit):
        if run.get("state") == "running":
            continue
        try:
            t = build(db, run["id"])
        except Exception:
            continue
        if t.divergence is not None or run.get("state") in ("failed", "interrupted"):
            out.append((run, t.divergence))
    return out
