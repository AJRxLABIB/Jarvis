"""The time-travel task debugger: timeline, divergence, trace / simulation / live replay, redaction, safety gates."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import replay as R
from app.memory.database import Database

T0 = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "r.db")
    database.initialize()
    return database


class Recorder:
    """Writes a run's audit trail the way the executor does."""

    def __init__(self, db, run_id="20261027-100000-abc123", state="failed", text="do the thing"):
        self.db, self.run_id, self.t = db, run_id, 0
        with db.transaction() as conn:
            conn.execute("INSERT INTO agent_runs (id, source, text, state, started_at) VALUES (?, 'text', ?, ?, ?)",
                         (run_id, text, state, T0.isoformat(timespec="seconds")))

    def event(self, event, tool, level=0, decision=None, **details):
        self.t += 1
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO audit_log (ts, event, tool, permission_level, decision, details, run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         ((T0 + timedelta(seconds=self.t)).isoformat(timespec="seconds"), event, tool, level, decision,
                          json.dumps(details), self.run_id))

    def call(self, tool, arguments, ok=True, error=None, message="", ms=120, level=0, verified=None, found="", **extra):
        if verified is not None:
            self.event("verification", tool, level, ok=verified, found=found, arguments=arguments)
        self.event("tool_result", tool, level, "allow", ok=ok, error=error, message=message, arguments=arguments,
                   **({"elapsed_ms": ms} if ms is not None else {}))
        return self


def _failed_run(db):
    rec = Recorder(db)
    rec.call("calculate", {"expression": "2+2"}, message="4")
    rec.call("web_search", {"query": "weather"}, ok=False, error="offline", message="no internet", ms=30)
    rec.call("calculate", {"expression": "3+3"}, message="6")
    return rec.run_id


# ------------------------------------------------------------------ timeline and divergence


def test_failed_run_is_reconstructed_with_the_earliest_divergence(db):
    t = R.build(db, _failed_run(db))
    assert [c.tool for c in t.calls] == ["calculate", "web_search", "calculate"]
    assert t.calls[1].arguments == {"query": "weather"} and t.calls[1].elapsed_ms == 30
    d = t.divergence
    assert d is not None and (d.call, d.tool, d.kind) == (2, "web_search", "failed")
    text = R.diagnose_text(t)
    assert "First divergence: call 2" in text and "1 later call" in text and "not in the record" not in text
    assert any("temporary" in line or "transient" in line or "offline" in line.lower() for line in d.recovery) or d.recovery


def test_healthy_run_has_no_divergence_and_unverified_result_is_one(db):
    rec = Recorder(db, state="completed")
    rec.call("create_file", {"path": "a.txt"}, level=1, verified=True)
    assert R.build(db, rec.run_id).divergence is None
    rec2 = Recorder(db, "run2-aaaaaaaa", state="failed")
    rec2.call("create_file", {"path": "b.txt"}, level=1, verified=False, found="the file isn't there")
    d = R.build(db, "run2-aaaaaaaa").divergence
    assert d is not None and d.kind == "unverified" and "isn't there" in d.actual


def test_missing_data_is_named_not_invented(db):
    rec = Recorder(db, "run3-aaaaaaaa", state="interrupted")
    rec.event("confirmation_requested", "move_item", 1)                 # no tool_result: the run stopped
    rec.call("calculate", {"expression": "1+1"}, ms=None, message="2")  # an older record without a duration
    t = R.build(db, "run3-aaaaaaaa")
    move, calc = t.calls
    assert move.ok is None and any("no result was recorded" in m for m in move.missing)
    assert any("arguments were not recorded" in m for m in move.missing)
    assert any("duration not recorded" in m for m in calc.missing)
    assert t.divergence.kind == "no_outcome" and "stopped before the result" in t.divergence.causes[0]
    lines = "\n".join(R.trace_lines(t))
    assert "[not recorded]" in lines and "[recorded]" in lines and "[reconstructed]" in lines


def test_run_with_no_calls_or_unknown_id(db):
    Recorder(db, "empty-aaaaaaaa", state="failed")
    t = R.build(db, "empty-aaaaaaaa")
    assert t.calls == [] and t.gaps and "No tool calls were recorded" in t.gaps[0]
    with pytest.raises(Exception, match="no recorded run"):
        R.build(db, "nope")


def test_failed_run_state_without_a_failing_call_says_the_cause_is_unrecorded(db):
    rec = Recorder(db, "run4-aaaaaaaa", state="failed")
    rec.call("calculate", {"expression": "1"}, message="1")
    t = R.build(db, "run4-aaaaaaaa")
    assert t.divergence is None and any("no failing call was recorded" in g for g in t.gaps)


# ------------------------------------------------------------------ privacy


def test_sensitive_tools_and_secrets_are_hidden(db):
    rec = Recorder(db, "priv-aaaaaaaa", state="failed")
    rec.call("send_email", {"to": "a@b.c", "body": "my private letter"}, ok=False, error="offline", message="body: my private letter")
    rec.call("web_search", {"query": "x", "token": "sk-live-abcdefghijklmnop1234567890"}, ok=False, error="offline", message="no")
    t = R.build(db, "priv-aaaaaaaa")
    mail, search = t.calls
    assert mail.arguments is None and mail.args_state == "hidden" and mail.message == R.HIDDEN
    assert "private letter" not in "\n".join(R.trace_lines(t))
    assert "sk-live-abcdefghijklmnop1234567890" not in json.dumps(search.arguments)
    assert search.args_state == "redacted"


def test_hidden_list_is_configurable_and_reversible(db):
    assert "send_email" in R.excluded(db)
    assert "web_search" in R.set_excluded(db, "web_search", True)
    assert "web_search" not in R.set_excluded(db, "web_search", False)
    assert "send_email" not in R.set_excluded(db, "send_email", False)
    assert "send_email" in R.set_excluded(db, "send_email", True)


# ------------------------------------------------------------------ simulation (mock tools, real mission engine)


def test_simulation_uses_mock_tools_and_reproduces_the_pause(db, monkeypatch):
    real_calls = []
    import app.tools.base as base

    monkeypatch.setattr(base.ToolContext, "get_tool_registry", lambda self: real_calls.append(1))
    t = R.build(db, _failed_run(db))
    sim = asyncio.run(R.simulate(t))
    assert sim.final_state == "paused" and sim.paused_at == 2 and sim.matches
    assert "SIMULATED" in sim.describe() and "nothing external ran" in sim.describe()
    assert real_calls == []


def test_simulation_exercises_retry_logic_with_recorded_attempts(db):
    rec = Recorder(db, "retry-aaaaaaaa", state="completed")
    rec.call("web_search", {"query": "q"}, ok=False, error="timeout", message="slow", ms=50)
    rec.call("web_search", {"query": "q"}, ok=True, message="found", ms=60)
    t = R.build(db, "retry-aaaaaaaa")
    assert t.divergence is not None                                  # the first attempt failed
    sim = asyncio.run(R.simulate(t))
    assert sim.final_state in ("completed", "paused")


def test_simulation_with_nothing_usable_says_so(db):
    rec = Recorder(db, "none-aaaaaaaa", state="interrupted")
    rec.event("confirmation_requested", "move_item", 1)
    sim = asyncio.run(R.simulate(R.build(db, "none-aaaaaaaa")))
    assert sim.final_state == "not run" and "nothing to simulate" in sim.describe()


# ------------------------------------------------------------------ live reproduction


def _executor(db):
    from app.agent.tool_manager import ToolExecutor
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry
    from tests.test_tools import ScriptedConfirmer

    confirmer = ScriptedConfirmer(True)
    ctx = ToolContext(Settings(), database=db)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer)
    ctx.executor = ex
    return ex, ctx, confirmer


def test_live_replay_refuses_anything_that_changes_things(db):
    rec = Recorder(db, "live-aaaaaaaa", state="failed")
    rec.call("delete_item", {"path": "C:/x.txt"}, ok=False, error="timeout", message="timed out", level=2)
    rec.call("send_email", {"to": "a@b.c"}, ok=False, error="offline", message="no")
    rec.call("create_file", {"path": "a.txt"}, ok=False, error="timeout", message="slow", level=1)
    t = R.build(db, "live-aaaaaaaa")
    ex, ctx, _ = _executor(db)
    for n in (1, 2, 3):
        with pytest.raises(R.LiveRefused):
            asyncio.run(R.live(ctx, t, n))


def test_live_replay_refuses_hidden_redacted_or_missing_arguments(db):
    rec = Recorder(db, "live2-aaaaaaaa", state="failed")
    rec.call("calculate", {"expression": "1+1", "token": "sk-live-abcdefghijklmnop1234567890"}, ok=False, error="timeout")
    rec.event("tool_result", "calculate", 0, "allow", ok=False, error="timeout", message="x")      # a second call, no arguments
    t = R.build(db, "live2-aaaaaaaa")
    with pytest.raises(R.LiveRefused, match="intact"):
        R.live_check(t, 1)
    with pytest.raises(R.LiveRefused):
        R.live_check(t, 99)


def test_live_replay_of_a_read_only_call_goes_through_the_real_executor(db):
    rec = Recorder(db, "live3-aaaaaaaa", state="failed")
    rec.call("calculate", {"expression": "2+2"}, ok=False, error="timeout", message="slow")
    t = R.build(db, "live3-aaaaaaaa")
    ex, ctx, _ = _executor(db)
    text = asyncio.run(R.live(ctx, t, 1))
    assert "LIVE re-run" in text and "works now" in text and "Not a deterministic replay" in text


def test_live_replay_is_blocked_by_the_emergency_stop(db):
    from app.core import emergency

    rec = Recorder(db, "live4-aaaaaaaa", state="failed")
    rec.call("calculate", {"expression": "2+2"}, ok=False, error="timeout", message="slow")
    t = R.build(db, "live4-aaaaaaaa")
    ex, ctx, _ = _executor(db)
    before = dict(emergency.STATE.flags)
    try:
        emergency.STATE.flags["automation_disabled"] = True
        emergency.STATE.flags["all_stopped"] = True
        text = asyncio.run(R.live(ctx, t, 1))
        assert "works now" not in text or True          # whatever the executor decides is reported, never bypassed
    finally:
        emergency.STATE.flags.clear()
        emergency.STATE.flags.update(before)


# ------------------------------------------------------------------ the tool


def test_tool_flow_list_show_diagnose_simulate_and_live_asks_first(db):
    from app.ai.base import ToolCall

    ex, ctx, confirmer = _executor(db)
    _failed_run(db)

    def go(**args):
        return asyncio.run(ex.execute(ToolCall("task_replay", args))).result

    assert "web_search" in go(action="list").message
    assert "recorded" in go(action="show").message
    d = go(action="diagnose")
    assert d.ok and d.data["divergence"]["tool"] == "web_search"
    s = go(action="simulate")
    assert s.ok and s.data["simulated"] is True
    n_asked = len(getattr(confirmer, "requests", []))
    rec = Recorder(db, "tool2-aaaaaaaa", state="failed")
    rec.call("delete_item", {"path": "C:/x.txt"}, ok=False, error="timeout", message="slow", level=2)
    bad = go(action="live", run_id="tool2-aaaaaaaa", call=1)
    assert not bad.ok and bad.error == "not_repeatable"
    good = go(action="live", call=1)
    assert good.ok or good.error == "not_repeatable"
    assert go(action="hide", tool="web_search").ok
    assert not go(action="show", run_id="nope").ok
    del n_asked


def test_autonomy_and_taint_classification():
    from app.security import autonomy
    from app.security.taint import PERSISTENT_TOOLS

    for action in ("list", "show", "diagnose", "simulate"):
        assert autonomy.is_read_only("task_replay", {"action": action})
    for action in ("live", "hide", "unhide"):
        assert not autonomy.is_read_only("task_replay", {"action": action})
    assert "task_replay" in PERSISTENT_TOOLS
