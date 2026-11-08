"""Look inside a plugin WITHOUT running it in Jarvis: import it in a separate, restricted child process first.

Before this, trusting a plugin imported (= ran) its code inside Jarvis to read its tool list. Now that happens in a child started with
``python -I -B`` (no user site / env / bytecode), a wall-clock timeout, a trimmed environment (no tokens, no PATH extras) and an audit hook that BLOCKS
network, process starts, native-library loads, registry / file writes and deletes while the module is imported. What the plugin tried is reported;
a plugin that tries any of it during import is refused for trusting.

Limits, honestly: this is import-time containment, not a full sandbox. It does not constrain what the tools do LATER when called inside Jarvis
(they still run with your rights after you trust them), a determined plugin can detect the probe and behave until trusted, and on Windows the Job Object limits (512 MB, no child processes, kill-on-close) are best-effort and unverified. It stops the casual harm (a plugin that does something at import) and shows you what to look at.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

CHILD = Path(__file__).with_name("plugin_probe_child.py")
ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 10.0
KEEP_ENV = ("SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL")


class ProbeError(Exception):
    pass


MEMORY_LIMIT_MB = 512


def limit_windows(proc: "subprocess.Popen") -> Any:
    """Put the child in a Windows Job Object: ``MEMORY_LIMIT_MB`` per process, ONE active process (so it cannot start others), killed when the job closes.
    Returns the job handle, or None where this does not apply / fails (then only the timeout and the audit hook protect). There is a tiny window between the
    process starting and joining the job; an import-time attack inside it is stopped by the audit hook, not by the job. Unverified on real Windows."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        LIMIT_ACTIVE_PROCESS, LIMIT_PROCESS_MEMORY, LIMIT_KILL_ON_CLOSE = 0x8, 0x100, 0x2000
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = Extended()
        info.Basic.LimitFlags = LIMIT_ACTIVE_PROCESS | LIMIT_PROCESS_MEMORY | LIMIT_KILL_ON_CLOSE
        info.Basic.ActiveProcessLimit = 2          # 2, not 1: a venv python.exe is a launcher that starts the real interpreter as its child (1 made it "Unable to create process")
        info.ProcessMemoryLimit = MEMORY_LIMIT_MB * 1024 * 1024
        ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))           # JobObjectExtendedLimitInformation
        if not ok or not k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):      # type: ignore[attr-defined]
            k32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def close_job(job: Any) -> None:
    if job:
        try:
            import ctypes

            ctypes.WinDLL("kernel32").CloseHandle(job)
        except Exception:
            pass


def probe(plugin: Path, timeout: float = TIMEOUT_S) -> dict:
    """-> {"tools": {name: level}, "version", "doc", "blocked": []}. Raises ProbeError when it timed out, crashed, was refused or is not a valid plugin."""
    plugin = Path(plugin)
    if not plugin.is_file():
        raise ProbeError("no such plugin file")
    env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
    path = os.pathsep.join(p for p in sys.path if p and Path(p).is_absolute() and Path(p).resolve() != plugin.parent.resolve())
    try:
        proc = subprocess.Popen([sys.executable, "-I", "-B", str(CHILD), str(ROOT), str(plugin), path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=env, cwd=str(plugin.parent), shell=False)
    except OSError as exc:
        raise ProbeError(f"could not start the probe ({exc})") from exc
    job = limit_windows(proc)                                      # Windows: memory cap, no child processes, killed with us (None elsewhere)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise ProbeError(f"it ran longer than {timeout:.0f} s while loading (stopped)") from exc
    finally:
        close_job(job)
    done = type("Done", (), {"stdout": out, "stderr": err})
    lines = [ln for ln in (done.stdout or "").splitlines() if ln.startswith("{")]
    if not lines:
        raise ProbeError("the probe produced no answer: " + ((done.stderr or "").strip().splitlines() or ["unknown error"])[-1][:160])
    try:
        info = json.loads(lines[-1])
    except ValueError as exc:
        raise ProbeError("the probe answered with something unreadable") from exc
    blocked = list(dict.fromkeys(info.get("blocked") or []))
    if blocked:
        raise ProbeError("it tried to do this while loading, which is blocked: " + ", ".join(blocked[:5]))
    if not info.get("ok"):
        raise ProbeError(str(info.get("error") or "it is not a valid plugin"))
    for name, level in (info.get("tools") or {}).items():
        if int(level) >= 3:
            raise ProbeError(f"{name} is CRITICAL: plugins can't have blocked-level tools")
    return info
