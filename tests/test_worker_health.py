from __future__ import annotations

import asyncio
from datetime import timedelta

from app.automation import scheduler as sched
from app.automation.triggers import ManualTrigger
from app.core import metrics
from tests.test_automation import NOW, Clock, db, scheduler_for, steps  # noqa: F401


def test_backpressure_refuses_extra_runs(db, monkeypatch) -> None:  # noqa: F811
    calls, clock = [], Clock(NOW)
    scheduler, _, _ = scheduler_for(db, calls, clock)
    task, _ = scheduler.store.save("one", ManualTrigger(), steps("step_one"), None)
    monkeypatch.setattr(sched, "MAX_CONCURRENT_RUNS", 2)
    scheduler._running.update({"a", "b"})
    report = asyncio.run(scheduler.run(task, manual=True))
    assert not report.ok and "Too many" in report.message and calls == []
    assert scheduler.health()["refused_runs"] == 1
    scheduler._running.clear()
    assert asyncio.run(scheduler.run(task, manual=True)).ok


def test_health_reports_stalled_and_clears(db) -> None:  # noqa: F811
    calls, clock = [], Clock(NOW)
    scheduler, _, _ = scheduler_for(db, calls, clock)
    assert scheduler.health()["loop_alive"] is False and scheduler.health()["running"] == []
    scheduler._running.add("t1")
    scheduler._started["t1"] = NOW - timedelta(minutes=45)
    health = scheduler.health()
    assert health["stalled"] == [{"task": "t1", "seconds": 2700}]
    findings = metrics.analyse_workers(health)
    assert [f.kind for f in findings].count("worker_stalled") == 1
    assert any(f.kind == "worker_stopped" for f in findings)


def test_run_forgets_its_start_time(db) -> None:  # noqa: F811
    calls, clock = [], Clock(NOW)
    scheduler, _, _ = scheduler_for(db, calls, clock)
    task, _ = scheduler.store.save("one", ManualTrigger(), steps("step_one"), None)
    asyncio.run(scheduler.run(task, manual=True))
    assert scheduler._started == {} and scheduler._running == set()
    asyncio.run(scheduler.tick())
    assert scheduler.health()["seconds_since_last_check"] == 0


def test_no_workers_no_findings() -> None:
    assert metrics.analyse_workers(None) == []
    assert metrics.analyse_workers({"loop_alive": True, "stalled": [], "refused_runs": 0}) == []
