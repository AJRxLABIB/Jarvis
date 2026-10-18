"""The dashboard's process list, measured in a small helper PROCESS (``--stats-child``), not a thread.

Listing every process (Chrome alone has dozens) with its CPU and memory keeps Python busy for a large part of a second
on Windows. In a worker THREAD that still froze the 3D core for ~0.3-0.7 s every six seconds: Python threads share one
interpreter lock, and psutil's calls hold it, so the window's thread (which draws every frame) had to wait. A separate
process has its own interpreter: the window never waits for it.

Protocol (one line each way): Jarvis -> helper ``procs <cores>``; helper -> Jarvis a JSON list of [cpu, rss, name].
The helper runs at below-normal priority and exits when Jarvis closes its stdin. If it can't start, the old in-thread
measurement is used (so the numbers never disappear).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

from app.utils.logging_setup import get_logger

_log = get_logger("ui.stats")


# ------------------------------------------------------------------ the child
def run_stats_child() -> int:
    from app.ui.stats_sampler import busiest_apps

    try:
        import psutil

        me = psutil.Process()
        me.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 10)
    except Exception:
        pass
    for line in sys.stdin:
        parts = line.split()
        if not parts or parts[0] != "procs":
            continue
        try:
            cores = int(parts[1]) if len(parts) > 1 else 1
        except ValueError:
            cores = 1
        rows = busiest_apps(cores)
        sys.stdout.write(json.dumps(rows if rows is not None else None) + "\n")
        sys.stdout.flush()
    return 0


def child_command() -> list[str]:
    from app.utils.paths import is_frozen, project_root

    if is_frozen():
        return [sys.executable, "--stats-child"]
    return [sys.executable, str(project_root() / "run.py"), "--stats-child"]


# ------------------------------------------------------------------ the parent side
class StatsHelper:
    """Asks the helper process; called from the dashboard's worker thread (reading the pipe doesn't hold the lock)."""

    def __init__(self, popen=subprocess.Popen) -> None:
        self._popen = popen
        self.process: subprocess.Popen | None = None
        self.failed = False
        self._lock = threading.Lock()

    def _start(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        if self.failed:
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self.process = self._popen(child_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, bufsize=1, encoding="utf-8",
                                       creationflags=flags)
            return True
        except OSError:
            _log.warning("the stats helper could not start; measuring in a thread", exc_info=True)
            self.failed = True
            return False

    def busiest(self, cores: int) -> list[tuple[float, float, str]] | None:
        """The busiest apps, or None when the helper isn't available (the caller then measures itself)."""
        with self._lock:
            if not self._start():
                return None
            try:
                self.process.stdin.write(f"procs {int(cores)}\n")
                self.process.stdin.flush()
                line = self.process.stdout.readline()
                if not line:
                    raise OSError("the stats helper stopped")
                rows = json.loads(line)
            except (OSError, ValueError):
                self.close()
                self.failed = True
                return None
            return [(float(c), float(r), str(n)) for c, r, n in rows] if rows else []

    def close(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            try:
                process.stdin.close()
            except Exception:
                pass
            try:
                process.wait(2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
