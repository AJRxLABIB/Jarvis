"""Supervisor for the optional Windows service (packaging\\jarvis_service.py).

The service does NOT contain a second copy of the automation engine. It starts ``run.py --background`` (the same host,
the same scheduler, the same permission checks, the same audit log) as a child process and keeps it alive:

* the child exits 0  -> it was told to stop (the ``background_mode`` tool, or the window took over): not restarted;
* the child exits 3  -> the window (or another host) holds the lock: try again in 60 s;
* the child crashes  -> restart after 5 s, doubling up to 5 min; after 5 crashes within 10 min the supervisor gives up
  and stays idle (it never restart-loops), leaving the evidence in ``data\\logs``;
* the service is stopped -> the child is first asked to stop through its control file, then terminated, then killed.

The supervisor is plain Python with injected ``spawn``/``wait`` so its decisions are tested without Windows.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

RETRY_LOCKED_SECONDS = 60.0
BASE_BACKOFF = 5.0
MAX_BACKOFF = 300.0
CRASH_WINDOW = 600.0
MAX_CRASHES = 5
POLL_SECONDS = 2.0
GRACEFUL_STOP_SECONDS = 20.0


class Child(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def python_for(root: Path) -> Path:
    """The interpreter that runs Jarvis: the project's .venv (no console window), else the one running this code.
    Inside a pywin32 service ``sys.executable`` is pythonservice.exe, which cannot run run.py."""
    for name in ("pythonw.exe", "python.exe"):
        candidate = Path(root) / ".venv" / "Scripts" / name
        if candidate.exists():
            return candidate
    return Path(sys.exec_prefix) / ("pythonw.exe" if sys.platform == "win32" else "bin/python3")


def service_command(root: Path) -> list[str]:
    return [str(python_for(root)), str(Path(root) / "run.py"), "--background"]


def spawn_child(root: Path) -> subprocess.Popen:
    return subprocess.Popen(service_command(root), cwd=str(root), shell=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def ask_to_stop(data_dir: Path) -> None:
    """The same control file the background_mode tool writes (app/automation/background.py)."""
    try:
        (Path(data_dir) / "background.control").write_text(json.dumps({"command": "stop"}), encoding="utf-8")
    except OSError:
        pass


class Supervisor:
    def __init__(self, spawn: Callable[[], Child], wait: Callable[[float], bool], *, data_dir: Path,
                 log: Callable[[str], None] = lambda message: None, clock: Callable[[], float] = time.monotonic) -> None:
        self.spawn, self.wait, self.data_dir, self.log, self.clock = spawn, wait, Path(data_dir), log, clock
        self.crashes: list[float] = []
        self.starts = 0

    def _stop_child(self, child: Child) -> None:
        ask_to_stop(self.data_dir)
        try:
            child.wait(GRACEFUL_STOP_SECONDS)
            return
        except Exception:
            pass
        child.terminate()
        try:
            child.wait(10)
        except Exception:
            child.kill()

    def _idle(self) -> None:
        while not self.wait(POLL_SECONDS * 2.5):
            pass

    def run(self) -> None:
        """Blocks until ``wait`` reports the service was asked to stop. ``wait(seconds)`` returns True on stop."""
        while True:
            child = self.spawn()
            self.starts += 1
            self.log(f"started background host (start #{self.starts})")
            while child.poll() is None:
                if self.wait(POLL_SECONDS):
                    self.log("service stopping: asking the background host to stop")
                    self._stop_child(child)
                    return
            code = child.returncode
            if code == 0:
                self.log("background host stopped normally (asked to stop or the window took over); not restarting")
                self._idle()
                return
            if code == 3:
                self.log(f"another Jarvis holds the lock (the window is open?); retrying in {RETRY_LOCKED_SECONDS:.0f} s")
                delay = RETRY_LOCKED_SECONDS
            else:
                now = self.clock()
                self.crashes = [t for t in self.crashes if now - t < CRASH_WINDOW] + [now]
                if len(self.crashes) >= MAX_CRASHES:
                    self.log(f"background host crashed {len(self.crashes)} times in {CRASH_WINDOW / 60:.0f} minutes "
                             f"(last exit code {code}); giving up - see data\\logs, then restart the service")
                    self._idle()
                    return
                delay = min(MAX_BACKOFF, BASE_BACKOFF * 2 ** (len(self.crashes) - 1))
                self.log(f"background host crashed (exit code {code}); restarting in {delay:.0f} s")
            if self.wait(delay):
                return


def status_line(data_dir: Path, now: float | None = None) -> dict[str, Any]:
    """What the host itself says (data\\background.json); used by the service script's status command."""
    try:
        return json.loads((Path(data_dir) / "background.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "no state file"}
