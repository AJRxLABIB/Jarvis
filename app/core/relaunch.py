"""Restart Jarvis by itself ("Hey Jarvis, restart yourself" / "refresh"), so new code is picked up without rebooting the PC.

The replacement is started FIRST but told to wait until this process has ended (`--restart-after <pid>`), so the two never
run together and the single-instance lock is free when the new one starts. No shell is involved: a fixed argument list.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - fixed argument list, shell=False
import sys
from pathlib import Path

from app.utils.paths import is_frozen, project_root


def relaunch_command(pid: int, *, minimized: bool, frozen: bool | None = None, executable: str | None = None,
                     root: Path | None = None) -> list[str]:
    frozen = is_frozen() if frozen is None else frozen
    exe = Path(executable or sys.executable)
    args = [] if frozen else [str((root or project_root()) / "run.py")]
    if not frozen:
        windowless = exe.with_name("pythonw.exe")           # no console window
        if windowless.is_file():
            exe = windowless
    args += ["--restart-after", str(int(pid))]
    if minimized:
        args.append("--minimized")
    return [str(exe), *args]


def start_replacement(pid: int, *, minimized: bool) -> None:
    """Start the new Jarvis (it waits for ``pid`` to exit). Raises OSError when it cannot be started."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen(relaunch_command(pid, minimized=minimized), shell=False, close_fds=True,  # noqa: S603
                     creationflags=flags, cwd=str(project_root()))


def wait_for_exit(pid: int, timeout: float = 30.0) -> bool:
    """True once process ``pid`` is gone (or the timeout passed)."""
    import time

    import psutil

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------- `python run.py --restart` (from a terminal)
RESTART_REQUEST = "restart.request"
REQUEST_MAX_AGE = 60.0


def request_restart(data_dir: Path) -> Path:
    """Ask the running window to restart itself (it checks for this file every couple of seconds)."""
    import json
    import os
    import time

    path = Path(data_dir) / RESTART_REQUEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"by": os.getpid(), "at": time.time()}), encoding="utf-8")
    return path


def take_request(data_dir: Path, now: float | None = None) -> bool:
    """True (once) when a fresh restart request is waiting. Old requests are thrown away, never acted on."""
    import json
    import time

    path = Path(data_dir) / RESTART_REQUEST
    if not path.is_file():
        return False
    try:
        at = float(json.loads(path.read_text(encoding="utf-8")).get("at", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        at = 0.0
    try:
        path.unlink()
    except OSError:
        return False
    return 0 <= ((time.time() if now is None else now) - at) <= REQUEST_MAX_AGE


def lock_pid(lock_path: Path) -> int | None:
    """The PID written in the single-instance lock file (QLockFile: first line), if any."""
    try:
        first = Path(lock_path).read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
        return int(first) if first.isdigit() else None
    except (OSError, IndexError):
        return None


def is_jarvis(pid: int) -> bool:
    """Only ever touch a process that really is Jarvis (run.py, or the packaged JARVIS.exe)."""
    import psutil

    try:
        proc = psutil.Process(pid)
        cmd = " ".join(proc.cmdline()).lower()
        name = proc.name().lower()
    except (psutil.Error, OSError):
        return False
    return "run.py" in cmd or name.startswith("jarvis") or "app.main" in cmd


def _is_background_host(pid: int) -> bool:
    import psutil

    try:
        return "--background" in psutil.Process(pid).cmdline()
    except (psutil.Error, OSError):
        return False


def cli_restart(data_dir: Path, lock_path: Path, *, minimized: bool = False, wait: float = 20.0,
                start=None, waiter=None) -> tuple[bool, str]:
    """Stop the running Jarvis (politely: it restarts its own way; forced only if it doesn't answer) and start a
    fresh one. Returns (ok, message)."""
    import os

    import psutil

    start = start or (lambda: start_replacement(os.getpid(), minimized=minimized))
    waiter = waiter or wait_for_exit
    pid = lock_pid(lock_path)
    if pid and pid != os.getpid() and psutil.pid_exists(pid) and is_jarvis(pid):
        from app.automation import background

        request = request_restart(data_dir)                 # the window restarts itself when it sees this
        if _is_background_host(pid):
            background.request(data_dir, "stop")             # a background host is asked to stop instead
        stopped = waiter(pid, wait)
        taken = not request.exists()                         # the window took the request: it starts its own successor
        if taken and stopped:
            return True, "Jarvis restarted."
        request.unlink(missing_ok=True)
        message = "Jarvis restarted."
        if not stopped:
            try:
                proc = psutil.Process(pid)
                proc.terminate()
                proc.wait(5)
            except psutil.TimeoutExpired:
                proc.kill()
            except psutil.Error:
                pass
            if psutil.pid_exists(pid) and is_jarvis(pid):
                return False, f"Jarvis (process {pid}) didn't stop, so I didn't start a second copy."
            message = "Jarvis didn't answer, so it was stopped and started again."
            if taken:
                return True, message + " (It had already started its replacement.)"
        try:
            start()
        except OSError as exc:
            return False, f"Jarvis stopped but couldn't be started again: {exc}"
        return True, message
    try:
        start()
    except OSError as exc:
        return False, f"Jarvis wasn't running and couldn't be started: {exc}"
    return True, "Jarvis wasn't running; started it."
