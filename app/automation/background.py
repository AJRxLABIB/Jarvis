"""Optional background mode: run the scheduler without the window (``python run.py --background``).

This is NOT a second task engine. It hosts the same ``Scheduler`` (same store, same checkpoints, same recovery, same
emergency switches, same ToolExecutor with its permission checks and audit log) in a headless process.

What is different, on purpose:

* **Nothing needing a human is approved.** There is no dialog, so every confirmation is refused. A step that needs one
  fails and is recorded; it is never silently allowed. Voice, the phone remote and the screen are not started.
* **One engine at a time.** It takes the same per-user lock as the window, so the two can never both run automations. If
  you open the window while it runs, the window asks it to stop and then takes over (see ``hand_over``).
* **Visible and controllable.** ``data\\background.json`` shows its state and last heartbeat; the ``background_mode`` tool
  (status / pause / resume / stop) writes a control file it reads every few seconds. The emergency switches, which live
  in the database, are honoured too.
* **Never started silently.** Nothing installs or starts it. ``packaging\\background.ps1 -Install`` (run by you, no
  administrator needed) registers a per-user logon task and ``-Uninstall`` removes it.
* **Restart recovery is the scheduler's own**: runs found ``running`` after a crash are marked interrupted, missed one-off
  tasks within the grace period run once, nothing irreversible is repeated blindly.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILE = "background.json"
CONTROL_FILE = "background.control"
HEARTBEAT_STALE_SECONDS = 90
COMMANDS = ("pause", "resume", "stop")


def instance_lock_path() -> Path:
    """The same per-user lock the window uses (app/ui/app.py): window and background host exclude each other."""
    return Path(tempfile.gettempdir()) / "JarvisAssistant.instance.lock"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)     # readers see the old file or the new one, never half of it


def read_state(data_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((Path(data_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        return False


def is_running(data_dir: Path, now: datetime | None = None, pid_alive: Callable[[int], bool] | None = None) -> bool:
    """A background host is running: its state says so, its process exists and its heartbeat is recent."""
    pid_alive = pid_alive or _pid_alive
    state = read_state(data_dir)
    if not state or state.get("status") not in ("running", "paused"):
        return False
    try:
        beat = datetime.fromisoformat(state["heartbeat"])
    except (KeyError, ValueError):
        return False
    return (now or _now()) - beat < timedelta_seconds(HEARTBEAT_STALE_SECONDS) and pid_alive(int(state.get("pid", 0)))


def timedelta_seconds(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def request(data_dir: Path, command: str) -> None:
    """Ask the running host to pause, resume or stop. Written as a file the host reads; safe if no host is running."""
    if command not in COMMANDS:
        raise ValueError(command)
    _write_json(Path(data_dir) / CONTROL_FILE, {"command": command, "at": _now().isoformat(timespec="seconds")})


def hand_over(data_dir: Path, try_lock: Callable[[], bool], *, wait_seconds: float = 15.0, poll: float = 0.5,
              sleep: Callable[[float], None] = time.sleep) -> bool:
    """The window was opened while a background host holds the lock: ask it to stop, then take the lock.

    Returns True when the lock was acquired. Returns False if nothing could be handed over (the other process is a
    second window, or the host did not stop in time) - the caller then says "already running", as before.
    """
    if not is_running(data_dir):
        return False
    request(data_dir, "stop")
    waited = 0.0
    while waited < wait_seconds:
        if try_lock():
            return True
        sleep(poll)
        waited += poll
    return try_lock()


class BackgroundHost:
    def __init__(self, data_dir: Path, scheduler: Any, *, version: str = "", pid: int | None = None,
                 clock: Callable[[], datetime] = _now) -> None:
        self.data_dir, self.scheduler, self.version = Path(data_dir), scheduler, version
        self.pid = pid if pid is not None else os.getpid()
        self.clock = clock
        self.status = "starting"
        self.started_at = clock().isoformat(timespec="seconds")
        self.notices: list[str] = []
        self.stop_requested = False

    # ----------------------------------------------------------- state and control

    def notify(self, title: str, message: str) -> None:
        """The scheduler's notification hook: no popup exists here, so it is recorded where you can read it."""
        self.notices = [*self.notices, f"{self.clock().strftime('%H:%M')} {title}: {message}"[:200]][-20:]

    def write_state(self) -> None:
        health: dict[str, Any] = {}
        try:
            health = self.scheduler.health()
        except Exception:
            pass
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.data_dir / STATE_FILE, {
            "status": self.status, "pid": self.pid, "version": self.version, "started_at": self.started_at,
            "heartbeat": self.clock().isoformat(timespec="seconds"), "running_now": health.get("running", []),
            "stalled": health.get("stalled", []), "refused_runs": health.get("refused_runs", 0), "notices": self.notices,
            "note": "Steps that need your approval are refused here; open the window to run them."})

    def _read_command(self) -> str | None:
        path = self.data_dir / CONTROL_FILE
        try:
            command = json.loads(path.read_text(encoding="utf-8")).get("command")
        except (OSError, ValueError):
            return None
        try:
            path.unlink()                 # consumed: a command is acted on once
        except OSError:
            pass
        return command if command in COMMANDS else None

    def cycle(self) -> bool:
        """One look at the control file plus a heartbeat. Returns False once the host should end."""
        command = self._read_command()
        if command == "stop":
            self.stop_requested = True
        elif command == "pause" and self.status == "running":
            self.status = "paused"
            self.notify("Background", "paused: no automation starts until you resume")
        elif command == "resume" and self.status == "paused":
            self.status = "running"
            self.notify("Background", "resumed")
        self.write_state()
        return not self.stop_requested

    async def run(self, poll: float = 2.0) -> None:
        """Run until stopped. Pausing stops the scheduler loop; a run already in progress finishes its current step."""
        self.status = "running"
        self.scheduler.start()
        running = True
        try:
            self.write_state()
            while True:
                keep_going = self.cycle()
                if self.status == "paused" and running:
                    await self.scheduler.stop()
                    running = False
                elif self.status == "running" and not running:
                    self.scheduler.start()
                    running = True
                if not keep_going:
                    break
                await asyncio.sleep(poll)
        finally:
            await self.scheduler.stop()
            self.status = "stopped"
            self.write_state()


# ------------------------------------------------------------------------------ process entry point


def run_background(ctx: Any, version: str = "") -> int:
    """``python run.py --background``. Exit codes: 0 stopped normally, 3 another Jarvis already holds the lock."""
    from PySide6.QtCore import QLockFile

    lock = QLockFile(str(instance_lock_path()))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        print("Jarvis is already running (the window or another background host). Nothing was started.")
        return 3
    from app.automation.scheduler import Scheduler
    from app.automation.tasks import AutomationStore
    from app.cli import create_tool_executor
    from app.core import emergency
    from app.core.upkeep import Upkeep
    from app.memory.settings_store import SettingsStore
    from app.security.confirmations import DenyConfirmer

    emergency.STATE.load(SettingsStore(ctx.database))
    executor = create_tool_executor(ctx, DenyConfirmer())
    host_holder: dict[str, BackgroundHost] = {}
    scheduler = Scheduler(AutomationStore(ctx.database), executor,
                          lambda title, message: host_holder["host"].notify(title, message))
    executor.context.scheduler = scheduler
    upkeep = Upkeep(ctx.database, ctx.paths.db_path, ctx.paths.data_dir / "backups", settings=ctx.settings)
    scheduler.housekeeping.append(upkeep.run)
    host = host_holder["host"] = BackgroundHost(ctx.paths.data_dir, scheduler, version=version)
    print(f"Jarvis background mode started (pid {host.pid}). Pause/stop it with the 'background_mode' tool, or "
          "open the window - it takes over. Ctrl+C stops it.")
    try:
        asyncio.run(host.run())
    except KeyboardInterrupt:
        pass
    finally:
        lock.unlock()
    print("Jarvis background mode stopped.")
    return 0
