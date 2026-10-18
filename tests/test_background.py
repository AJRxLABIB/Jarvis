from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.automation import background as bg
from app.config.settings import Settings
from app.tools.automation import BackgroundArgs, background_mode
from app.tools.base import ToolContext
from app.utils.paths import AppPaths

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class FakeScheduler:
    def __init__(self) -> None:
        self.starts = self.stops = 0

    def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1

    def health(self) -> dict:
        return {"running": [{"task": "t1", "seconds": 5}], "stalled": [], "refused_runs": 2}


def host(tmp_path: Path, clock=lambda: NOW) -> bg.BackgroundHost:
    return bg.BackgroundHost(tmp_path, FakeScheduler(), version="9.9", pid=4242, clock=clock)


def test_state_file_shows_status_heartbeat_and_running_work(tmp_path: Path) -> None:
    h = host(tmp_path)
    h.status = "running"
    h.notify("Reminder", "stand up")
    h.write_state()
    state = bg.read_state(tmp_path)
    assert state["status"] == "running" and state["pid"] == 4242 and state["running_now"][0]["task"] == "t1"
    assert state["refused_runs"] == 2 and "stand up" in state["notices"][0] and "approval" in state["note"]
    assert not list(tmp_path.glob("*.tmp"))                      # written atomically


def test_pause_resume_stop_commands_are_consumed_once(tmp_path: Path) -> None:
    h = host(tmp_path)
    h.status = "running"
    bg.request(tmp_path, "pause")
    assert h.cycle() is True and h.status == "paused" and not (tmp_path / bg.CONTROL_FILE).exists()
    assert h.cycle() is True and h.status == "paused"            # not repeated: the file is gone
    bg.request(tmp_path, "resume")
    assert h.cycle() is True and h.status == "running"
    bg.request(tmp_path, "stop")
    assert h.cycle() is False and h.stop_requested


def test_unknown_or_garbled_commands_are_ignored(tmp_path: Path) -> None:
    h = host(tmp_path)
    h.status = "running"
    (tmp_path / bg.CONTROL_FILE).write_text(json.dumps({"command": "format_disk"}), encoding="utf-8")
    assert h.cycle() is True and h.status == "running"
    (tmp_path / bg.CONTROL_FILE).write_text("not json", encoding="utf-8")
    assert h.cycle() is True
    with pytest.raises(ValueError):
        bg.request(tmp_path, "format_disk")


def test_run_loop_starts_pauses_resumes_and_stops_the_scheduler(tmp_path: Path) -> None:
    h = host(tmp_path)
    sched = h.scheduler

    async def drive() -> None:
        task = asyncio.create_task(h.run(poll=0.01))
        await asyncio.sleep(0.05)
        bg.request(tmp_path, "pause")
        await asyncio.sleep(0.08)
        paused = (h.status, sched.stops)
        bg.request(tmp_path, "resume")
        await asyncio.sleep(0.08)
        resumed = (h.status, sched.starts)
        bg.request(tmp_path, "stop")
        await asyncio.wait_for(task, 2)
        assert paused == ("paused", 1) and resumed == ("running", 2)

    asyncio.run(drive())
    assert h.status == "stopped" and bg.read_state(tmp_path)["status"] == "stopped" and sched.stops >= 2


def test_is_running_needs_state_heartbeat_and_a_live_process(tmp_path: Path) -> None:
    assert bg.is_running(tmp_path) is False                       # no state at all
    h = host(tmp_path)
    h.status = "running"
    h.write_state()
    assert bg.is_running(tmp_path, NOW + timedelta(seconds=10), pid_alive=lambda pid: True) is True
    assert bg.is_running(tmp_path, NOW + timedelta(seconds=10), pid_alive=lambda pid: False) is False   # process gone
    assert bg.is_running(tmp_path, NOW + timedelta(minutes=10), pid_alive=lambda pid: True) is False    # stale heartbeat
    h.status = "stopped"
    h.write_state()
    assert bg.is_running(tmp_path, NOW, pid_alive=lambda pid: True) is False


def test_hand_over_asks_the_host_to_stop_then_takes_the_lock(tmp_path: Path, monkeypatch) -> None:
    h = host(tmp_path, clock=lambda: datetime.now(timezone.utc))
    h.status = "running"
    h.write_state()
    monkeypatch.setattr(bg, "_pid_alive", lambda pid: True)
    attempts = []

    def try_lock() -> bool:
        attempts.append(1)
        return len(attempts) >= 3                                   # the host releases the lock on the 3rd try

    assert bg.hand_over(tmp_path, try_lock, sleep=lambda s: None) is True
    assert json.loads((tmp_path / bg.CONTROL_FILE).read_text())["command"] == "stop"


def test_hand_over_does_nothing_when_no_host_runs_or_it_will_not_stop(tmp_path: Path, monkeypatch) -> None:
    assert bg.hand_over(tmp_path, lambda: pytest.fail("must not try"), sleep=lambda s: None) is False   # a second window
    h = host(tmp_path, clock=lambda: datetime.now(timezone.utc))
    h.status = "running"
    h.write_state()
    monkeypatch.setattr(bg, "_pid_alive", lambda pid: True)
    assert bg.hand_over(tmp_path, lambda: False, wait_seconds=1.0, poll=0.5, sleep=lambda s: None) is False


def test_the_window_and_the_host_share_one_lock() -> None:
    from app.ui.app import instance_lock_path

    assert bg.instance_lock_path() == instance_lock_path()


def test_host_refuses_to_start_when_another_jarvis_holds_the_lock(tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtCore import QLockFile

    monkeypatch.setattr(bg, "instance_lock_path", lambda: tmp_path / "x.lock")
    first = QLockFile(str(tmp_path / "x.lock"))
    assert first.tryLock(100)
    try:
        assert bg.run_background(object()) == 3                      # never touches ctx: it exits before doing anything
    finally:
        first.unlock()


def test_tool_status_and_control(tmp_path: Path, monkeypatch) -> None:
    paths = AppPaths.build(tmp_path)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(Settings(), paths=paths)
    assert "No background host" in background_mode(BackgroundArgs(action="status"), ctx).message
    assert "Nothing to stop" in background_mode(BackgroundArgs(action="stop"), ctx).message
    assert not (paths.data_dir / bg.CONTROL_FILE).exists()           # nothing written when no host runs
    h = bg.BackgroundHost(paths.data_dir, FakeScheduler(), pid=1, clock=lambda: datetime.now(timezone.utc))
    h.status = "running"
    h.write_state()
    monkeypatch.setattr(bg, "_pid_alive", lambda pid: True)
    assert "is running" in background_mode(BackgroundArgs(action="status"), ctx).message
    assert background_mode(BackgroundArgs(action="pause"), ctx).data["requested"] == "pause"
    assert json.loads((paths.data_dir / bg.CONTROL_FILE).read_text())["command"] == "pause"


def test_workflows_cannot_control_the_host() -> None:
    from app.automation.tasks import FORBIDDEN_STEP_TOOLS

    assert "background_mode" in FORBIDDEN_STEP_TOOLS


def test_install_script_is_opt_in_and_per_user() -> None:
    text = (Path(__file__).resolve().parent.parent / "packaging" / "background.ps1").read_text(encoding="utf-8")
    assert "-RunLevel Limited" in text and "-AtLogOn" in text and "RunLevel Highest" not in text
    assert "if ($Install)" in text and "param([switch]$Install" in text    # does nothing unless asked
