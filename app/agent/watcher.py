"""PC watcher: notices problems before you do and offers the fix by voice.

* the system disk is almost full -> "say 'clean temp files'"
* one app has used most of the CPU for minutes -> "say 'force quit <app>'"
* the GPU is running hot
* a big download just finished in Downloads
Each warning is given once (again only after it cleared, or a day later). All checks are local.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psutil

DISK_FREE_PERCENT = 10
CPU_HOG_PERCENT = 70  # of the whole CPU
CPU_HOG_MINUTES = 2
GPU_HOT_C = 85
DOWNLOAD_MIN_MB = 50
REPEAT_SECONDS = 24 * 3600
_IGNORED = frozenset({"system idle process", "idle", "system", "memory compression", "registry", "jarvis",
                      "python", "pythonw", "ollama", "ollama app"})


@dataclass
class Watcher:
    downloads: Path | None = None
    clock: Callable[[], float] = time.time
    disk: Callable[[], float] | None = None  # free percent of the system drive
    processes: Callable[[], dict[str, float]] | None = None  # app -> CPU % of the whole CPU
    gpu_temperature: Callable[[], float | None] | None = None
    _warned: dict[str, float] = field(default_factory=dict)
    _hog_since: dict[str, float] = field(default_factory=dict)
    _seen_downloads: dict[str, tuple[int, float]] | None = None

    def _once(self, key: str) -> bool:
        now = self.clock()
        if now - self._warned.get(key, -1e18) < REPEAT_SECONDS:
            return False
        self._warned[key] = now
        return True

    def check(self) -> list[str]:
        """One round of checks; returns the warnings to say (usually none)."""
        messages = []
        for check in (self._check_disk, self._check_cpu, self._check_gpu, self._check_downloads):
            try:
                message = check()
            except Exception:
                message = None
            if message:
                messages.append(message)
        return messages

    def _check_disk(self) -> str | None:
        free = self.disk() if self.disk is not None else _disk_free_percent()
        if free is None:
            return None
        if free >= DISK_FREE_PERCENT + 3:
            self._warned.pop("disk", None)
            return None
        if free < DISK_FREE_PERCENT and self._once("disk"):
            return (f"Your system drive is almost full: {free:.0f}% free. Say 'clean temp files' and I'll free "
                    "some space, or 'open storage' to see what's using it.")
        return None

    def _check_cpu(self) -> str | None:
        usage = self.processes() if self.processes is not None else _process_cpu()
        now = self.clock()
        for name in list(self._hog_since):
            if usage.get(name, 0) < CPU_HOG_PERCENT:
                del self._hog_since[name]
        for name, percent in usage.items():
            if percent < CPU_HOG_PERCENT or name.lower() in _IGNORED:
                continue
            since = self._hog_since.setdefault(name, now)
            if now - since >= CPU_HOG_MINUTES * 60 and self._once(f"cpu:{name}"):
                return (f"{name} has been using {percent:.0f}% of the processor for a few minutes. Say 'force quit "
                        f"{name}' if it's stuck.")
        return None

    def _check_gpu(self) -> str | None:
        temperature = self.gpu_temperature() if self.gpu_temperature is not None else _gpu_temperature()
        if temperature is None:
            return None
        if temperature < GPU_HOT_C - 5:
            self._warned.pop("gpu", None)
        elif temperature >= GPU_HOT_C and self._once("gpu"):
            return f"The GPU is running hot: {temperature:.0f} degrees. Check the fans or give it a break."
        return None

    def _check_downloads(self) -> str | None:
        folder = self.downloads
        if folder is None or not folder.is_dir():
            return None
        current: dict[str, tuple[int, float]] = {}
        for entry in os.scandir(folder):
            if entry.is_file() and not entry.name.lower().endswith((".crdownload", ".part", ".tmp", ".partial")):
                stat = entry.stat()
                current[entry.name] = (stat.st_size, stat.st_mtime)
        previous, self._seen_downloads = self._seen_downloads, current
        if previous is None:
            return None  # first look: nothing is "new"
        done = [name for name, (size, _) in current.items()
                if name not in previous and size >= DOWNLOAD_MIN_MB * 1024 * 1024]
        if done and self._once(f"download:{done[0]}"):
            return f"Your download {done[0]} has finished. Say 'open downloads' to see it."
        return None


def _disk_free_percent() -> float | None:
    drive = (os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else "/"
    usage = psutil.disk_usage(drive)
    return 100 - usage.percent


def _process_cpu() -> dict[str, float]:
    cores = psutil.cpu_count() or 1
    usage: dict[str, float] = {}
    for proc in psutil.process_iter(["name", "cpu_percent"]):
        name = (proc.info.get("name") or "").removesuffix(".exe")
        if name:
            usage[name] = usage.get(name, 0.0) + (proc.info.get("cpu_percent") or 0.0) / cores
    return usage


def _gpu_temperature() -> float | None:
    from app.tools.system import gpu_info

    gpus = gpu_info().get("gpus") or []
    return float(gpus[0]["temperature_c"]) if gpus else None
