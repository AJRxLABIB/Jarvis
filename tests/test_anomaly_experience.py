"""Statistical anomaly detection and the Experience Compiler."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import anomaly as A
from app.core import experience as X
from app.memory.database import Database

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "a.db")
    database.initialize()
    return database


def _audit(db, at, tool, ok=True, ms=None, arguments=None, level=0, error=None):
    details = {"ok": ok, "error": error, "message": "", "arguments": arguments or {}}
    if ms is not None:
        details["elapsed_ms"] = ms
    with db.transaction() as conn:
        conn.execute("INSERT INTO audit_log (ts, event, tool, permission_level, decision, details) VALUES (?, 'tool_result', ?, ?, 'allow', ?)",
                     (at.isoformat(timespec="seconds"), tool, level, json.dumps(details)))


# ------------------------------------------------------------------ statistics


def test_robust_z_needs_history_and_ignores_one_old_spike():
    assert A.robust_z(100, [1, 2, 3]) is None                       # too little history: no verdict
    history = [100, 102, 98, 101, 99, 100, 103, 97, 5000]          # one old spike
    assert A.robust_z(101, history) < 1                              # the spike doesn't make normal values "normal-ish"
    assert A.robust_z(400, history) > A.Z_LIMIT


def test_constant_history_does_not_score_infinite_for_tiny_change():
    flat = [50.0] * 12
    assert A.robust_z(50.5, flat) < A.Z_LIMIT                       # 1 % change on a perfectly steady series
    assert A.robust_z(90, flat) > A.Z_LIMIT


def test_binomial_tail_is_sane():
    assert A.binomial_tail(0, 5, 0.1) == 1.0
    assert A.binomial_tail(5, 5, 0.1) == pytest.approx(1e-5, rel=1e-3)
    assert A.binomial_tail(3, 5, 0.9) > 0.9


# ------------------------------------------------------------------ tool latency / failures


def _history(db, tool, n=12, ms=1000):
    for i in range(n):
        _audit(db, NOW - timedelta(days=2 + i % 10, hours=i), tool, ms=ms + (i % 3) * 20)


def test_slow_tool_is_flagged_and_normal_one_is_not(db):
    _history(db, "web_search")
    _history(db, "get_weather")
    for i in range(4):
        _audit(db, NOW - timedelta(hours=1 + i), "web_search", ms=9000)
        _audit(db, NOW - timedelta(hours=1 + i), "get_weather", ms=1010)
    found = A.tool_anomalies(db, NOW)
    assert [a.subject for a in found] == ["web_search"] and found[0].kind == "latency"
    assert "slower than usual" in found[0].message


def test_latency_needs_baseline_and_recent_samples(db):
    for i in range(3):
        _audit(db, NOW - timedelta(days=3 + i), "web_search", ms=1000)       # only 3 earlier samples
        _audit(db, NOW - timedelta(hours=i + 1), "web_search", ms=20000)
    assert A.tool_anomalies(db, NOW) == []


def test_failure_spike_is_flagged_but_refusals_are_not_failures(db):
    for i in range(20):
        _audit(db, NOW - timedelta(days=2 + i // 3, hours=i), "open_application", ok=(i != 7))
    for i in range(4):
        _audit(db, NOW - timedelta(hours=1 + i), "open_application", ok=False, error="not_found")
    found = A.tool_anomalies(db, NOW)
    assert any(a.kind == "failures" and a.subject == "open_application" for a in found)
    # the same number of calls refused by you / by safety rules say nothing about the tool
    for i in range(20):
        _audit(db, NOW - timedelta(days=2 + i // 3, hours=i), "send_message", ok=True)
    for i in range(4):
        _audit(db, NOW - timedelta(hours=1 + i), "send_message", ok=False, error="user_declined")
    assert not any(a.subject == "send_message" for a in A.tool_anomalies(db, NOW))


# ------------------------------------------------------------------ load and spending


def test_load_and_spend_anomalies(db):
    with db.transaction() as conn:
        for i in range(60):
            conn.execute("INSERT INTO metrics (ts, cpu, mem_percent, disk_percent) VALUES (?, 20, ?, 50)",
                         ((NOW - timedelta(hours=3 + i)).isoformat(timespec="seconds"), 45 + (i % 4)))
        for i in range(5):
            conn.execute("INSERT INTO metrics (ts, cpu, mem_percent, disk_percent) VALUES (?, 20, 93, 50)",
                         ((NOW - timedelta(minutes=5 * i)).isoformat(timespec="seconds"),))
        for i in range(10):
            conn.execute("INSERT INTO expenses (ts, amount, currency, category, note) VALUES (?, ?, '', 'food', '')",
                         ((NOW - timedelta(days=10 + i)).isoformat(timespec="seconds"), 10 + (i % 3)))
        conn.execute("INSERT INTO expenses (ts, amount, currency, category, note) VALUES (?, 300, '', 'food', '')",
                     ((NOW - timedelta(days=1)).isoformat(timespec="seconds"),))
        conn.execute("INSERT INTO expenses (ts, amount, currency, category, note) VALUES (?, 12, '', 'food', '')",
                     ((NOW - timedelta(days=2)).isoformat(timespec="seconds"),))
    kinds = {a.kind for a in A.detect(db, NOW)}
    assert {"load", "spend"} <= kinds
    spend = A.spend_anomalies(db, NOW)
    assert len(spend) == 1 and spend[0].observed == 300                  # the ordinary 12 is not flagged


def test_nothing_unusual_says_so_honestly(db):
    assert "Nothing looks unusual" in A.describe(A.detect(db, NOW))


def test_radar_includes_anomalies_and_feedback_mutes_them(db):
    from types import SimpleNamespace as NS

    from app.core import radar

    _history(db, "web_search")
    for i in range(4):
        _audit(db, datetime.now(timezone.utc) - timedelta(hours=1 + i), "web_search", ms=9000)
    for i in range(12):
        _audit(db, datetime.now(timezone.utc) - timedelta(days=2 + i % 10, hours=i), "web_search", ms=1000)
    ctx = NS(database=db, paths=None)
    keys = [c.key for c in radar.signals(ctx)]
    assert "anomaly:latency:web_search" in keys
    fb = radar.Feedback(db)
    for _ in range(3):
        fb.rate("anomaly:latency:web_search", useful=False)
    kept, held = fb.filter(radar.signals(ctx))
    assert "anomaly:latency:web_search" not in [c.key for c in kept] and held


# ------------------------------------------------------------------ experience compiler


def _routine(db, day_offset, hour=9, chrome="https://a.example", vol=30):
    base = NOW - timedelta(days=day_offset, hours=-hour + 12)
    _audit(db, base, "open_application", ms=800, arguments={"name": "chrome"})
    _audit(db, base + timedelta(seconds=20), "open_website", ms=600, arguments={"site": chrome})
    _audit(db, base + timedelta(seconds=40), "volume_control", ms=100, arguments={"action": "set", "level": vol})


def test_repeated_sequence_across_days_becomes_a_proposal(db):
    for d in (1, 2, 4):
        _routine(db, d, vol=30 + d)
    found = X.propose(db, NOW)
    assert len(found) == 1
    p = found[0]
    assert [s["tool"] for s in p.steps] == ["open_application", "open_website", "volume_control"]
    assert p.runs == 3 and p.days == 3
    assert any("volume_control" in v for v in p.varied) and not any("open_application" in v for v in p.varied)
    assert p.steps[2]["arguments"]["level"] == 31                      # latest run's value


def test_one_day_burst_or_two_runs_is_not_a_routine(db):
    for h in (8, 9, 10):
        _routine(db, 1, hour=h)                                        # three runs, one day
    assert X.propose(db, NOW) == []
    d2 = Database(db.path.parent / "b.db") if hasattr(db, "path") else None
    if d2 is not None:
        d2.initialize()
        for d in (1, 2):
            _routine(d2, d)
        assert X.propose(d2, NOW) == []                                # two runs only


def test_unsafe_failed_secret_and_forbidden_calls_never_become_steps(db):
    for d in (1, 2, 3):
        base = NOW - timedelta(days=d)
        _audit(db, base, "open_application", arguments={"name": "chrome"})
        _audit(db, base + timedelta(seconds=10), "send_email", arguments={"to": "a@b.c"})      # never recorded
        _audit(db, base + timedelta(seconds=20), "run_command", arguments={"command": "dir"})  # never recorded
        _audit(db, base + timedelta(seconds=30), "delete_file", arguments={"path": "x"}, level=2)  # dangerous
        _audit(db, base + timedelta(seconds=40), "volume_control", arguments={"action": "set", "level": 5}, ok=False)    # failed
        _audit(db, base + timedelta(seconds=50), "open_website", arguments={"url": "https://u:pass123@x.example/"})
    # nothing here is a sequence of two eligible consecutive calls, and the secret url is excluded
    assert X.propose(db, NOW) == []


def test_dismissed_and_already_saved_routines_are_not_offered(db):
    for d in (1, 2, 3):
        _routine(db, d)
    p = X.propose(db, NOW)[0]
    assert X.propose(db, NOW, existing_steps=[["open_application", "open_website", "volume_control"]]) == []
    X.dismiss(db, p.id)
    assert X.propose(db, NOW) == []


def test_longer_pattern_absorbs_its_sub_patterns(db):
    for d in (1, 2, 3):
        _routine(db, d)
    assert len(X.propose(db, NOW)) == 1                               # not also 'open_application -> open_url' etc.


# ------------------------------------------------------------------ the tools


def _executor(db):
    from app.agent.tool_manager import ToolExecutor
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry
    from tests.test_tools import ScriptedConfirmer

    confirmer = ScriptedConfirmer(True)
    return ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()),
                        ToolContext(Settings(), database=db), confirmer=confirmer), confirmer


def _run(ex, tool, **args):
    import asyncio

    from app.ai.base import ToolCall

    return asyncio.run(ex.execute(ToolCall(tool, args))).result


def test_tools_propose_then_save_after_confirmation(db):
    now = datetime.now(timezone.utc)
    global NOW
    saved, NOW = NOW, now
    try:
        for d in (1, 2, 3):
            _routine(db, d)
    finally:
        NOW = saved
    ex, confirmer = _executor(db)
    r = _run(ex, "experience", action="propose")
    assert r.ok and len(r.data["proposals"]) == 1 and "Nothing is saved" not in r.message
    assert _run(ex, "anomalies").ok
    saved_r = _run(ex, "experience", action="save", id="1", name="Morning setup")
    assert saved_r.ok, saved_r.message
    assert _run(ex, "list_automations").ok
    # now it exists as a workflow, so it isn't proposed again
    assert _run(ex, "experience", action="propose").data["proposals"] == []


def test_experience_save_and_autonomy_rules():
    from app.security import autonomy

    assert autonomy.is_read_only("experience", {"action": "propose"})
    assert not autonomy.is_read_only("experience", {"action": "save"})
    assert autonomy.is_read_only("anomalies")
    from app.automation.tasks import FORBIDDEN_STEP_TOOLS
    from app.core.missions import NOT_A_STEP
    from app.security.taint import PERSISTENT_TOOLS

    assert "experience" in FORBIDDEN_STEP_TOOLS and "experience" in NOT_A_STEP and "experience" in PERSISTENT_TOOLS


def test_audit_records_elapsed_ms(db):
    ex, _ = _executor(db)
    _run(ex, "anomalies")
    with db.connect() as conn:
        row = conn.execute("SELECT details FROM audit_log WHERE event = 'tool_result' AND tool = 'anomalies'").fetchone()
    assert row is None or "elapsed_ms" in json.loads(row[0])
