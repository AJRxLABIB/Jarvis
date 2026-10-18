"""System measurements for the dashboard, taken OFF the window's thread.

Listing every process (Chrome alone has dozens) with its CPU and memory takes a noticeable part of
a second on Windows; done on the window's thread it froze every animation for ~0.7 s every six
seconds. Here everything slow happens in a worker thread; the window only receives the numbers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import psutil

IGNORED_PROCESSES = frozenset({"system idle process", "idle", "system", "registry", "memory compression"})


@dataclass
class Sample:
    cpu: float = 0.0
    cores: int = 0
    freq_ghz: float | None = None
    memory: Any = None  # psutil.virtual_memory() result
    battery: Any = None  # psutil.sensors_battery() result or None
    disk: Any = None  # psutil.disk_usage() result or None
    net: tuple[float, int, int] | None = None  # (monotonic time, bytes sent, bytes received)
    boot_time: float = 0.0
    pids: int | None = None
    processes: list[tuple[float, float, str]] | None = None  # (cpu share, rss, name), busiest first
    taken_s: float = 0.0
    errors: list[str] = field(default_factory=list)


def take(drive: str, with_processes: bool, helper=None) -> Sample:
    """One measurement. Never raises: a part that fails is left empty."""
    started = time.perf_counter()
    s = Sample()
    s.cpu = psutil.cpu_percent(None)
    s.cores = psutil.cpu_count() or 0
    try:
        freq = psutil.cpu_freq()
        s.freq_ghz = freq.current / 1000 if freq and freq.current else None
    except Exception:
        pass
    s.memory = psutil.virtual_memory()
    try:
        s.battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    except Exception:
        s.battery = None
    try:
        s.disk = psutil.disk_usage(drive)
    except OSError:
        s.disk = None
    try:
        counters = psutil.net_io_counters()
        s.net = (time.monotonic(), counters.bytes_sent, counters.bytes_recv)
    except Exception:
        s.net = None
    try:
        s.boot_time = psutil.boot_time()
    except Exception:
        pass
    try:
        s.pids = len(psutil.pids())
    except Exception:
        s.pids = None
    if with_processes:
        rows = helper.busiest(s.cores) if helper is not None else None    # another process: never starves the window
        s.processes = rows if rows is not None else busiest_apps(s.cores)
    s.taken_s = time.perf_counter() - started
    return s


def busiest_apps(cores: int) -> list[tuple[float, float, str]] | None:
    """The apps using the most CPU, one entry per app (Chrome's many processes count as one). Names only."""
    apps: dict[str, list[float]] = {}
    try:
        for proc in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
            info = proc.info
            name = (info.get("name") or "").removesuffix(".exe")
            if not name or name.lower() in IGNORED_PROCESSES:
                continue
            memory = info.get("memory_info")
            total = apps.setdefault(name, [0.0, 0.0])
            total[0] += (info.get("cpu_percent") or 0.0) / max(1, cores)
            total[1] += memory.rss if memory else 0
    except Exception:
        return None
    return sorted(((cpu, rss, name) for name, (cpu, rss) in apps.items()), reverse=True)
