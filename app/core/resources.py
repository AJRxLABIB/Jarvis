"""Resource orchestration for a small GPU (RTX 4050, 6 GB): what is loaded, what still fits, and who may start now.

Builds on what exists - ``core/perf.py`` (RAM / CPU / battery / VRAM snapshot, performance modes, ``fit_check``), ``ai/ollama.py`` (load / unload / ``/api/ps``)
and ``core/modes.py`` (gaming / meeting / power saving) - and adds the missing coordination layer:

* ``read_gpu``         - GPU memory, utilisation, temperature, power: NVML when the optional ``pynvml`` package is installed, else ``nvidia-smi`` (reviewed
                         path in tools/system.py), else "no NVIDIA GPU read" (CPU-only machines are fine). Nothing here is mandatory.
* ``plan_load``        - will a model fit with a configurable safety margin? Uses the OBSERVED memory of a model when Jarvis has seen it loaded, and only
                         then the file size x headroom: a download size is not a runtime size.
* ``ModelBook``        - what Ollama has loaded right now (``/api/ps``) and the largest GPU / RAM use ever observed per model (kept in the settings store).
* ``idle_candidates``  - which loaded models may be unloaded to make room: idle longest first, never the one in use, never one a running workload uses.
* ``WorkloadScheduler``- P0 emergency / P1 interaction / P2 requested / P3 background / P4 optional; a priority queue with a concurrency limit, VRAM
                         reservations, timeouts, cancellation, and a "hold" that parks low-priority work while gaming / presenting (they run later, nothing is
                         dropped). It does not pre-empt a running job (a model is never unloaded mid-request).

Honest status: pure logic + tests with fake readers. The scheduler is NOT yet wired into every inference path (conversation, voice, indexing); until it is,
callers opt in by running their work through it. Real GPU behaviour is unverified on your hardware.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

P0, P1, P2, P3, P4 = range(5)
PRIORITY_NAMES = {P0: "P0 emergency", P1: "P1 interaction", P2: "P2 requested", P3: "P3 background", P4: "P4 optional"}
DEFAULT_MARGIN_GB = 0.6          # VRAM kept free for the display, the speech model and spikes on a 6 GB card
HEADROOM = 1.15                  # runtime memory vs file size, only used when nothing has been observed
OBSERVED_KEY = "model_observed"


# ---- GPU ----------------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Gpu:
    name: str = ""
    total_gb: float | None = None
    used_gb: float | None = None
    util_pct: float | None = None
    temp_c: float | None = None
    power_w: float | None = None
    source: str = "none"             # nvml | nvidia-smi | none

    @property
    def free_gb(self) -> float | None:
        return None if self.total_gb is None or self.used_gb is None else max(0.0, self.total_gb - self.used_gb)


def _nvml() -> Gpu | None:
    try:
        import pynvml                                        # optional: never required
    except ImportError:
        return None
    try:
        pynvml.nvmlInit()
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            name = pynvml.nvmlDeviceGetName(h)
            util = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
            temp = pynvml.nvmlDeviceGetTemperature(h, 0)
            try:
                power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
            except Exception:
                power = None
            return Gpu(name.decode() if isinstance(name, bytes) else str(name), mem.total / 2**30, mem.used / 2**30, float(util), float(temp), power, "nvml")
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return None


def _smi() -> Gpu | None:
    try:
        from app.tools.system import gpu_info

        gpus = gpu_info().get("gpus") or []
        if not gpus:
            return None
        g = max(gpus, key=lambda x: float(x["memory_total_mb"]))
        return Gpu(g["name"], float(g["memory_total_mb"]) / 1024, float(g["memory_used_mb"]) / 1024, float(g["utilization_percent"]),
                   float(g["temperature_c"]), None, "nvidia-smi")
    except Exception:
        return None


def read_gpu(readers: tuple[Callable[[], Gpu | None], ...] | None = None) -> Gpu:
    for reader in readers or (_nvml, _smi):
        found = reader()
        if found is not None:
            return found
    return Gpu()


# ---- fitting -----------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LoadPlan:
    verdict: str                     # gpu | cpu | no | unknown
    need_gb: float | None
    why: str


def plan_load(model_gb: float | None, gpu: Gpu, ram_free_gb: float | None, observed_gb: float | None = None, margin_gb: float = DEFAULT_MARGIN_GB) -> LoadPlan:
    """Where could this model run right now? ``observed_gb`` (measured when it was loaded before) beats the file size."""
    if observed_gb:
        need, basis = observed_gb, "measured when it was loaded before"
    elif model_gb:
        need, basis = model_gb * HEADROOM, f"file size x {HEADROOM} (an estimate: nothing measured yet)"
    else:
        return LoadPlan("unknown", None, "its size is unknown")
    free = gpu.free_gb
    if free is not None and need + margin_gb <= free:
        return LoadPlan("gpu", need, f"{need:.1f} GB needed ({basis}) + {margin_gb:.1f} GB safety margin fits in {free:.1f} GB free GPU memory")
    if ram_free_gb is not None and need <= ram_free_gb:
        gp = f"GPU has {free:.1f} GB free" if free is not None else "no GPU memory was read"
        return LoadPlan("cpu", need, f"{need:.1f} GB needed ({basis}); {gp}, so it would run on the CPU in system memory ({ram_free_gb:.1f} GB free): slower")
    return LoadPlan("no", need, f"{need:.1f} GB needed ({basis}) does not fit in GPU memory or free RAM; close programs or pick a smaller model")


# ---- loaded models ----------------------------------------------------------------------------------------------------------------------

@dataclass
class Loaded:
    name: str
    size_gb: float
    vram_gb: float
    expires_at: str = ""

    @property
    def gpu_share(self) -> float:
        return min(1.0, self.vram_gb / self.size_gb) if self.size_gb else 0.0


def parse_ps(data: Any) -> list[Loaded]:
    out = []
    for e in (data or {}).get("models", []) or []:
        if not (e.get("name") or e.get("model")):
            continue
        try:
            out.append(Loaded(str(e.get("name") or e.get("model")), float(e.get("size") or 0) / 2**30, float(e.get("size_vram") or 0) / 2**30, str(e.get("expires_at") or "")))
        except (TypeError, ValueError):
            continue
    return out


class ModelBook:
    """Largest observed memory per model, kept in the settings store (``model_observed``). Observations only ever come from ``/api/ps``."""

    def __init__(self, store: Any, clock: Callable[[], float] = time.time) -> None:
        self.store, self.clock = store, clock

    def observe(self, loaded: list[Loaded]) -> None:
        data = dict(self.store.get(OBSERVED_KEY, {}) or {})
        for m in loaded:
            row = data.get(m.name, {})
            data[m.name] = {"total_gb": round(max(m.size_gb, row.get("total_gb", 0.0)), 2), "vram_gb": round(max(m.vram_gb, row.get("vram_gb", 0.0)), 2),
                            "last_seen": self.clock()}
        self.store.set(OBSERVED_KEY, dict(sorted(data.items(), key=lambda kv: -kv[1]["last_seen"])[:40]))

    def observed_gb(self, name: str) -> float | None:
        row = (self.store.get(OBSERVED_KEY, {}) or {}).get(name)
        return float(row["total_gb"]) if row and row.get("total_gb") else None

    def history(self) -> dict[str, dict]:
        return dict(self.store.get(OBSERVED_KEY, {}) or {})


def idle_candidates(loaded: list[Loaded], last_used: dict[str, float], busy: set[str], now: float, idle_after_s: float = 600.0, keep: set[str] | None = None) -> list[str]:
    """Models that may be unloaded: not busy, not kept, idle for at least ``idle_after_s``; the longest idle first."""
    keep = keep or set()
    idle = [(now - last_used.get(m.name, 0.0), m.name) for m in loaded if m.name not in busy and m.name not in keep]
    return [n for age, n in sorted(idle, reverse=True) if age >= idle_after_s]


# ---- scheduler ---------------------------------------------------------------------------------------------------------------------------

@dataclass
class Job:
    id: int
    priority: int
    name: str
    work: Callable[[], Awaitable[Any]]
    vram_gb: float
    timeout: float | None
    future: "asyncio.Future[Any]"
    state: str = "queued"            # queued | running | done | failed | cancelled | timeout
    task: "asyncio.Task | None" = None
    loop: Any = None
    submitted: float = field(default_factory=time.monotonic)
    started: float | None = None
    ended: float | None = None


class WorkloadScheduler:
    def __init__(self, max_concurrent: int = 2, vram_budget: Callable[[], float | None] = lambda: None, margin_gb: float = DEFAULT_MARGIN_GB) -> None:
        self.max_concurrent, self.vram_budget, self.margin_gb = max_concurrent, vram_budget, margin_gb
        self._queue: list[Job] = []
        self._running: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._hold_from: int | None = None
        self._lock = threading.RLock()                       # hold() is called from the background worker thread
        self.history: list[Job] = []

    # -- control
    def hold(self, from_priority: int | None) -> None:
        """Park every queued job with priority >= ``from_priority`` (e.g. 3 while gaming); None releases them."""
        with self._lock:
            self._hold_from = from_priority
        self._pump()

    @property
    def reserved_gb(self) -> float:
        return sum(j.vram_gb for j in self._running.values())

    def busy_names(self) -> set[str]:
        return {j.name for j in self._running.values()}

    def stats(self) -> dict[str, Any]:
        return {"queued": [(PRIORITY_NAMES[j.priority], j.name) for j in sorted(self._queue, key=lambda j: (j.priority, j.id))],
                "running": [(PRIORITY_NAMES[j.priority], j.name, j.vram_gb) for j in self._running.values()],
                "reserved_gb": round(self.reserved_gb, 2), "held_from": self._hold_from, "max_concurrent": self.max_concurrent}

    # -- submit / cancel
    def submit(self, priority: int, name: str, work: Callable[[], Awaitable[Any]], vram_gb: float = 0.0, timeout: float | None = None) -> Job:
        if priority not in PRIORITY_NAMES:
            raise ValueError("priority must be 0..4")
        loop = asyncio.get_running_loop()
        job = Job(next(self._ids), priority, name[:80], work, max(0.0, vram_gb), timeout, loop.create_future(), loop=loop)
        with self._lock:
            self._queue.append(job)
        self._pump()
        return job

    def cancel(self, job: Job) -> bool:
        if job.state == "queued":
            self._queue.remove(job)
            self._finish(job, "cancelled")
            job.future.cancel()
            return True
        if job.state == "running" and job.task is not None:
            job.task.cancel()
            return True
        return False

    async def run(self, priority: int, name: str, work: Callable[[], Awaitable[Any]], vram_gb: float = 0.0, timeout: float | None = None) -> Any:
        return await self.submit(priority, name, work, vram_gb, timeout).future

    # -- internals
    def _fits(self, job: Job) -> bool:
        if job.priority == P0 or job.vram_gb <= 0:
            return True                                           # an emergency job is never held back by memory accounting
        free = self.vram_budget()
        if free is None:
            return True                                           # GPU memory unknown: do not invent a limit
        return self.reserved_gb + job.vram_gb + self.margin_gb <= free      # conservative: a running job's reservation may also already be counted in 'free'

    def _pump(self) -> None:
        with self._lock:
            for job in sorted(self._queue, key=lambda j: (j.priority, j.id)):
                if job.priority != P0 and len(self._running) >= self.max_concurrent:
                    continue
                if self._hold_from is not None and job.priority >= self._hold_from:
                    continue
                if not self._fits(job):
                    continue
                self._queue.remove(job)
                self._start(job)

    def _start(self, job: Job) -> None:
        """Always runs the job on the event loop it was submitted from (so futures and tasks stay on one loop, also when another thread pumps)."""
        job.state, job.started = "running", time.monotonic()
        self._running[job.id] = job
        try:
            same = asyncio.get_running_loop() is job.loop
        except RuntimeError:
            same = False
        if same:
            job.task = job.loop.create_task(self._execute(job))
        else:
            job.loop.call_soon_threadsafe(lambda: setattr(job, "task", job.loop.create_task(self._execute(job))))

    async def _execute(self, job: Job) -> None:
        try:
            result = await (asyncio.wait_for(job.work(), job.timeout) if job.timeout else job.work())
            self._finish(job, "done")
            if not job.future.done():
                job.future.set_result(result)
        except asyncio.TimeoutError:
            self._finish(job, "timeout")
            if not job.future.done():
                job.future.set_exception(TimeoutError(f"'{job.name}' ran longer than {job.timeout:.0f} s and was stopped"))
        except asyncio.CancelledError:
            self._finish(job, "cancelled")
            if not job.future.done():
                job.future.cancel()
        except Exception as exc:
            self._finish(job, "failed")
            if not job.future.done():
                job.future.set_exception(exc)

    def _finish(self, job: Job, state: str) -> None:
        with self._lock:
            job.state, job.ended = state, time.monotonic()
            self._running.pop(job.id, None)
            self.history = (self.history + [job])[-50:]
        self._pump_soon()

    def _pump_soon(self) -> None:
        try:
            asyncio.get_running_loop().call_soon(self._pump)
        except RuntimeError:
            self._pump()


_SCHEDULER: WorkloadScheduler | None = None


def scheduler() -> WorkloadScheduler:
    """The one shared scheduler. VRAM budget = free GPU memory from the performance snapshot (None = unknown: no invented limit)."""
    global _SCHEDULER
    if _SCHEDULER is None:
        def budget() -> float | None:
            try:
                from app.core import perf

                return perf.read_snapshot().vram_free_gb
            except Exception:
                return None

        _SCHEDULER = WorkloadScheduler(max_concurrent=3, vram_budget=budget)
    return _SCHEDULER


def apply_mode_hold(allowed_background: bool, allowed_heavy: bool) -> None:
    """Called by the background upkeep: gaming / presenting / meeting / power saving park P3-P4 work (and P2-P4 heavy work is not parked: you asked for it)."""
    scheduler().hold(None if allowed_background and allowed_heavy else P3)
