"""PC health history, findings, quiet hours for spoken reminders, and the daily automatic backup."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agent.shortcuts import match_shortcut
from app.config.settings import Settings
from app.core import metrics, proactive
from app.core.upkeep import Upkeep
from app.memory import backup
from app.memory.database import Database
from app.tools.base import ToolContext
from app.tools.diagnostics_tools import pc_health

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "assistant.db")
    database.initialize()
    return database


def reading(cpu=10.0, mem=40.0, disk=50.0, battery=None, plugged=None):
    return {"cpu": cpu, "mem_percent": mem, "disk_percent": disk, "battery": battery, "plugged": plugged}


def feed(store: metrics.MetricsStore, readings, start=T0):
    for i, r in enumerate(readings):
        assert store.record(r, start + timedelta(minutes=i))


def test_at_most_one_reading_a_minute_and_old_rows_are_forgotten(db: Database) -> None:
    store = metrics.MetricsStore(db)
    assert store.record(reading(), T0)
    assert not store.record(reading(), T0 + timedelta(seconds=20))  # too soon
    assert store.record(reading(), T0 + timedelta(minutes=2))
    store.record(reading(), T0 + timedelta(days=20))  # prunes everything older than 14 days
    assert len(store.history(24 * 30, T0 + timedelta(days=20))) == 1


def test_unavailable_values_are_stored_as_missing_not_invented(db: Database) -> None:
    store = metrics.MetricsStore(db)
    store.record(reading(battery=None, plugged=None), T0)
    row = store.history(1, T0 + timedelta(minutes=1))[0]
    assert row["battery"] is None and row["plugged"] is None
    text, _ = metrics.summary(db, T0 + timedelta(minutes=1))
    assert "battery" not in text and "not available" not in text.split("CPU")[0]


def test_sustained_cpu_memory_growth_disk_and_battery_findings(db: Database) -> None:
    store = metrics.MetricsStore(db)
    rows = [reading(cpu=95, mem=50 + i * 0.5, disk=93, battery=15, plugged=False) for i in range(80)]
    feed(store, rows)
    kinds = {f.kind for f in metrics.analyse_history(store.history(2, T0 + timedelta(minutes=81)))}
    assert kinds == {"high_cpu", "memory_growth", "low_disk", "low_battery"}


def test_a_healthy_pc_has_no_findings_and_short_spikes_are_ignored(db: Database) -> None:
    store = metrics.MetricsStore(db)
    feed(store, [reading(cpu=95 if i in (3, 4) else 20) for i in range(30)])
    assert metrics.analyse_history(store.history(1, T0 + timedelta(minutes=31))) == []
    text, data = metrics.summary(db, T0 + timedelta(minutes=31))
    assert "No problems found" in text and data["readings"] == 30


def test_state_findings_failed_automation_stuck_mission_failing_model(db: Database) -> None:
    now = T0 + timedelta(hours=5)
    old = (T0).isoformat()
    with db.transaction() as conn:
        conn.execute("INSERT INTO automation_tasks (id, name, enabled, trigger_type, trigger_config, actions, "
                     "last_status, created_at, updated_at) VALUES ('a', 'backup docs', 1, 'manual', '{}', '[]', "
                     "'stopped at step 2', 'x', 'x')")
        conn.execute("INSERT INTO missions (id, title, state, created_at, updated_at) VALUES ('m', 'ship it', 'paused', ?, ?)",
                     (old, old))
    findings = metrics.analyse_state(db, now, {"qwen3:8b": {"skipped_for": 30, "failures": 3, "last_error": "timeout"}})
    kinds = {f.kind for f in findings}
    assert kinds == {"failed_automation", "stuck_mission", "model_failing"}
    assert all(f.suggestion for f in findings)  # every finding says what to do, and none acts by itself


def test_the_pc_health_tool_is_read_only_and_honest_when_empty(db: Database) -> None:
    ctx = ToolContext(Settings(), database=db)
    result = pc_health(__import__("app.tools.base", fromlist=["NoArgs"]).NoArgs(), ctx)
    assert result.ok and "no readings recorded yet" in result.message
    assert match_shortcut("how is my pc doing", lambda n: None).name == "pc_health"
    assert match_shortcut("is something wrong with my computer", lambda n: None).name == "pc_health"


# ------------------------------------------------------------------ upkeep and backups


def make_upkeep(db: Database, tmp_path: Path, now: datetime, **settings) -> Upkeep:
    return Upkeep(db, db.path, tmp_path / "backups", settings=Settings(**settings), sampler=lambda: reading(),
                  clock=lambda: now.replace(tzinfo=None))


def test_upkeep_records_and_backs_up_once_a_day(db: Database, tmp_path: Path) -> None:
    now = datetime(2026, 10, 1, 12, 0)
    upkeep = make_upkeep(db, tmp_path, now)
    first = upkeep.run_once()
    assert first == {"recorded": True, "backed_up": True}
    assert len(backup.list_backups(tmp_path / "backups")) == 1
    again = upkeep.run_once()
    assert again["backed_up"] is False  # a backup exists from today
    upkeep.clock = lambda: now + timedelta(hours=25)
    assert upkeep.run_once()["backed_up"] is True
    assert Database(backup.list_backups(tmp_path / "backups")[0]).integrity_check() == "ok"


def test_upkeep_respects_the_switches(db: Database, tmp_path: Path) -> None:
    upkeep = make_upkeep(db, tmp_path, datetime(2026, 10, 1, 12, 0), auto_backup=False, metrics_history=False)
    assert upkeep.run_once() == {"recorded": False, "backed_up": False}
    assert backup.list_backups(tmp_path / "backups") == []


def test_a_failing_chore_is_reported_not_raised(db: Database, tmp_path: Path) -> None:
    upkeep = make_upkeep(db, tmp_path, datetime(2026, 10, 1, 12, 0))
    upkeep.sampler = lambda: (_ for _ in ()).throw(RuntimeError("sensor exploded"))
    did = upkeep.run_once()  # must not raise
    assert did["recorded"] is False and "sensor exploded" in upkeep.last_error
    assert did["backed_up"] is True  # the other chore still ran


def test_the_scheduler_loop_runs_housekeeping_and_survives_its_failures(db: Database) -> None:
    from app.automation import scheduler as sched
    from tests.test_automation import Clock, NOW, scheduler_for

    scheduler, _, _ = scheduler_for(db, [], Clock(NOW))
    calls = []

    async def good():
        calls.append("good")

    async def bad():
        raise RuntimeError("boom")

    scheduler.housekeeping += [bad, good]

    async def go():
        sched.CHECK_SECONDS = 0.01
        scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()

    original = sched.CHECK_SECONDS
    try:
        asyncio.run(go())
    finally:
        sched.CHECK_SECONDS = original
    assert calls  # the bad chore did not stop the good one or the loop


# ------------------------------------------------------------------ quiet hours


def test_reminders_are_spoken_outside_quiet_hours_only() -> None:
    settings = Settings(quiet_hours="23:00-07:00")
    assert proactive.reminder_may_speak(settings, datetime(2026, 10, 1, 14, 0))
    assert not proactive.reminder_may_speak(settings, datetime(2026, 10, 1, 23, 30))
    assert not proactive.reminder_may_speak(settings, datetime(2026, 10, 2, 6, 59))
    assert proactive.reminder_may_speak(settings, datetime(2026, 10, 2, 7, 0))
    assert proactive.reminder_may_speak(Settings(quiet_hours=""), datetime(2026, 10, 1, 23, 30))  # none set


def test_a_quiet_hours_reminder_is_shown_but_not_spoken(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.core import events, modes
    from app.ui.controller_parts import background

    shown, spoken = [], []
    monkeypatch.setattr(events.BUS, "publish", lambda *a, **k: None)
    monkeypatch.setattr(modes.STATE, "speaks", lambda priority: True)
    fake = SimpleNamespace(reminder=SimpleNamespace(emit=lambda t, m: shown.append(m)),
                           tts=SimpleNamespace(speak=spoken.append), ctx=SimpleNamespace(settings=Settings(quiet_hours="00:00-23:59")))
    background.BackgroundMixin._notify(fake, "Reminder: stretch", "stretch")
    assert shown == ["stretch"] and spoken == []
    fake.ctx.settings = Settings(quiet_hours="")
    background.BackgroundMixin._notify(fake, "Reminder: stretch", "stretch")
    assert spoken == ["stretch"]
