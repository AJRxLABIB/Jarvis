from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from app.automation.triggers import ManualTrigger
from app.core import emergency
from app.security.command_executor import CommandExecutor
from tests.test_automation import NOW, Clock, db, scheduler_for, steps  # noqa: F401


@pytest.fixture(autouse=True)
def fresh_state():
    old = emergency.STATE
    emergency.STATE = emergency.Emergency()
    yield
    emergency.STATE = old


def test_emergency_stop_reaches_a_scheduled_run_already_in_progress(db) -> None:  # noqa: F811
    calls, clock = [], Clock(NOW)
    scheduler, notes, _ = scheduler_for(db, calls, clock)
    task, _ = scheduler.store.save("chain", ManualTrigger(), steps("step_one", "step_two", "step_three"), None)
    original = scheduler.executor.execute

    async def stopping(call, **kw):
        result = await original(call, **kw)
        if call.name == "step_one":
            emergency.STATE.set("automation_disabled", True, by_voice=True)  # the user hits the stop button mid-run
        return result

    scheduler.executor.execute = stopping
    report = asyncio.run(scheduler._run(task, manual=False))
    assert not report.ok and [c[0] for c in calls] == ["step_one"] and "emergency" in report.message


def test_a_manual_run_is_not_blocked_by_the_automation_switch(db) -> None:  # noqa: F811
    calls, clock = [], Clock(NOW)
    scheduler, _, _ = scheduler_for(db, calls, clock)
    task, _ = scheduler.store.save("chain", ManualTrigger(), steps("step_one"), None)
    emergency.STATE.set("automation_disabled", True, by_voice=True)
    assert asyncio.run(scheduler.run(task, manual=True)).ok  # a person asked for it; executor rules still apply


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX sleep process")
def test_stop_all_ends_background_commands(tmp_path: Path) -> None:
    executor = CommandExecutor()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
    executor._background[proc.pid] = proc
    assert len(executor.running()) == 1
    assert executor.stop_all() == 1
    proc.wait(timeout=10)
    assert proc.poll() is not None and executor.running() == [] and executor.stop_all() == 0
