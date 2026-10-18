"""Automation guards: an automation writing into the folder it watches doesn't re-fire itself; an event trigger that
keeps firing is held back (and reported once); event-started automations can wait out quiet hours."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.automation import scheduler as sched
from app.automation.triggers import EventTrigger
from tests.test_automation import NOW, Clock, db, scheduler_for, steps  # noqa: F401  (fixture + helpers)


def test_own_writes_dont_retrigger(db, tmp_path):  # noqa: F811
    calls, clock = [], Clock(NOW)
    scheduler, _, _ = scheduler_for(db, calls, clock)
    folder = tmp_path / "inbox"
    folder.mkdir()
    task, _ = scheduler.store.save("sort", EventTrigger(event="file_changed", value=str(folder)), steps("step_one"), None)

    real_run = scheduler.run

    async def run_and_write(t, **kw):
        report = await real_run(t, **kw)
        (folder / f"written-by-automation-{len(calls)}.txt").write_text("x")      # the automation changes the folder
        return report

    scheduler.run = run_and_write
    asyncio.run(scheduler.tick())                                  # baseline
    (folder / "new.pdf").write_text("x")
    asyncio.run(scheduler.tick())
    asyncio.run(scheduler.tick())                                  # its own file must not start it again
    asyncio.run(scheduler.tick())
    assert calls == [("step_one", "step_one")]


def test_rate_cap_holds_back_a_runaway_trigger(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sched, "MAX_EVENT_RUNS_PER_HOUR", 3)
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock)
    task, _ = scheduler.store.save("usb", EventTrigger(event="usb"), steps("step_one"), None)
    allowed = []
    for _ in range(5):
        allowed.append(scheduler._event_gate(task))
    assert allowed == [True, True, True, False, False]
    assert len([n for n in notes if "held back" in n[0]]) == 1                # reported once, not five times
    clock.now = NOW + timedelta(hours=1, minutes=1)
    assert scheduler._event_gate(task)                                         # the hour passed: allowed again


def test_quiet_hours_only_when_switched_on(db):  # noqa: F811
    calls, clock = [], Clock(NOW.replace(hour=23, minute=30))
    scheduler, _, _ = scheduler_for(db, calls, clock)
    task, _ = scheduler.store.save("usb", EventTrigger(event="usb"), steps("step_one"), None)
    settings = scheduler.executor.context.settings
    assert settings.quiet_hours == "23:00-07:00" and scheduler._event_gate(task)     # off by default
    scheduler.executor.context.settings = settings.model_copy(update={"automation_quiet_hours": True})
    assert not scheduler._event_gate(task)
    clock.now = NOW.replace(hour=9)
    assert scheduler._event_gate(task)
