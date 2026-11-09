"""One front door for AI work: every request gets an id, a priority, a memory estimate and an observable lifecycle.

Sits on top of ``core/resources.py`` (GPU read, ``plan_load``, the shared ``WorkloadScheduler``) and ``core/retry.py``. Lifecycle:
QUEUED -> RESOURCE_CHECK -> MODEL_PREPARATION -> RUNNING -> COMPLETED | FAILED | CANCELLED (PAUSED while the mode hold parks it).

* RESOURCE_CHECK: ``plan_load`` with the real GPU read. Fits -> GPU; does not fit but RAM does -> CPU fallback (``req.device_used == "cpu"``, the reason is kept);
  fits nowhere -> FAILED with a clear message (never a crash). No GPU read at all (a CPU-only machine) -> the CPU, if RAM allows; an unknown model size -> the request goes ahead, no limit is invented.
* MODEL_PREPARATION: one load at a time per model (a lock per model name): two requests for the same model never start two loads.
* Retries follow ``core/retry.py`` (transient / resource kinds only), bounded; cancellation reaches queued and running requests.
* History is bounded (200). ``snapshot()`` is what a dashboard or the ``inference_queue`` tool shows - real states only.

Honest status: logic + tests with fake readers. The conversation and voice paths are NOT yet routed through it (they call the model directly); indexing and
tune_ai already use the scheduler. Callers opt in with ``await gateway().run(request, work)``.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core import resources as R
from app.core import retry

QUEUED, RESOURCE_CHECK, MODEL_PREPARATION, RUNNING, PAUSED, COMPLETED, FAILED, CANCELLED = (
    "QUEUED", "RESOURCE_CHECK", "MODEL_PREPARATION", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED")
FINAL = {COMPLETED, FAILED, CANCELLED}
CAPABILITIES = {"chat", "voice", "stt", "tts", "vision", "embedding", "document", "background", "indexing", "evaluation"}
_IDS = itertools.count(1)


class ResourceError(RuntimeError):
    pass


@dataclass
class InferenceRequest:
    capability: str
    model: str = ""
    priority: int = R.P2
    est_gb: float = 0.0
    max_seconds: float | None = 120.0
    context_tokens: int = 0
    device: str = "auto"                  # auto | gpu | cpu
    output_format: str = "text"
    max_retries: int = 1
    id: str = field(default_factory=lambda: f"inf-{next(_IDS)}")
    state: str = QUEUED
    device_used: str = ""
    plan_why: str = ""
    error: str = ""
    attempts: int = 0
    events: list[tuple[str, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES:
            raise ValueError(f"capability must be one of {sorted(CAPABILITIES)}")
        if self.device not in ("auto", "gpu", "cpu"):
            raise ValueError("device must be auto, gpu or cpu")
        if self.priority not in R.PRIORITY_NAMES:
            raise ValueError("priority must be 0..4")
        self.max_retries = max(0, min(int(self.max_retries), 3))

    @property
    def latency_ms(self) -> int | None:
        if len(self.events) < 2 or self.state not in FINAL:
            return None
        return int((self.events[-1][1] - self.events[0][1]) * 1000)


class Gateway:
    def __init__(self, sched: R.WorkloadScheduler | None = None, gpu: Callable[[], R.Gpu] | None = None, ram_free_gb: Callable[[], float | None] = lambda: None,
                 prepare: Callable[[str, str], Awaitable[None]] | None = None, observed: Callable[[str], float | None] = lambda m: None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.sched, self.gpu, self.ram_free_gb = sched, gpu or R.read_gpu, ram_free_gb
        self.prepare, self.observed, self.sleep, self.clock = prepare, observed, sleep, clock
        self.history: deque[InferenceRequest] = deque(maxlen=200)
        self._active: dict[str, tuple[InferenceRequest, Any]] = {}
        self._loads: dict[str, asyncio.Lock] = {}
        self._lock = threading.RLock()
        self.observers: list[Callable[[InferenceRequest], None]] = []

    # -- state
    def _set(self, req: InferenceRequest, state: str) -> None:
        req.state = state
        req.events.append((state, self.clock()))
        for fn in list(self.observers):
            try:
                fn(req)
            except Exception:
                pass                                              # a broken observer must not break inference

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = [(r.id, r.capability, r.model, r.state, R.PRIORITY_NAMES[r.priority], r.device_used or r.device) for r, _ in self._active.values()]
            recent = [(r.id, r.capability, r.state, r.device_used, r.latency_ms, r.error[:80]) for r in list(self.history)[-10:]]
        return {"active": active, "recent": recent, "scheduler": (self.sched or R.scheduler()).stats()}

    # -- checks
    def check(self, req: InferenceRequest) -> None:
        """RESOURCE_CHECK: decides the device, or raises ResourceError."""
        if req.device == "cpu" or req.est_gb <= 0:
            req.device_used, req.plan_why = ("cpu" if req.device == "cpu" else req.device or "auto"), "no memory check needed" if req.est_gb <= 0 else "CPU requested"
            return
        plan = R.plan_load(req.est_gb, self.gpu(), self.ram_free_gb(), observed_gb=self.observed(req.model) if req.model else None)
        req.plan_why = plan.why
        if plan.verdict in ("gpu", "unknown"):
            req.device_used = "gpu" if plan.verdict == "gpu" else "auto"
        elif plan.verdict == "cpu" and req.device != "gpu":
            req.device_used = "cpu"                                # honest fallback: slower, and said so
        else:
            hint = " Try a shorter context, a smaller model or close other programs." if req.context_tokens > 2048 or plan.verdict == "no" else ""
            raise ResourceError(plan.why + hint)

    # -- run
    async def run(self, req: InferenceRequest, work: Callable[[InferenceRequest], Awaitable[Any]]) -> Any:
        sched = self.sched or R.scheduler()
        self._set(req, QUEUED)
        held = sched._hold_from is not None and req.priority >= sched._hold_from
        if held:
            self._set(req, PAUSED)

        async def job() -> Any:
            delay = 0.0
            while True:
                req.attempts += 1
                try:
                    self._set(req, RESOURCE_CHECK)
                    self.check(req)
                    self._set(req, MODEL_PREPARATION)
                    if self.prepare and req.model:
                        lock = self._loads.setdefault(req.model, asyncio.Lock())
                        async with lock:                           # one load per model at a time
                            await self.prepare(req.model, req.device_used)
                    self._set(req, RUNNING)
                    return await work(req)
                except (asyncio.CancelledError, ResourceError):
                    raise
                except Exception as exc:
                    kind = retry.classify(getattr(exc, "code", None), str(exc))
                    pol = retry.POLICIES.get(kind, retry.POLICIES["unknown"])
                    if req.attempts > req.max_retries or pol.max_attempts <= 1:
                        raise
                    delay = min(pol.base_seconds * pol.factor ** (req.attempts - 1), 5.0)
                    await self.sleep(delay)

        sjob = sched.submit(req.priority, f"{req.capability}:{req.model or '-'}", job, vram_gb=0.0 if req.device == "cpu" else req.est_gb, timeout=req.max_seconds)
        with self._lock:
            self._active[req.id] = (req, sjob)
        try:
            result = await sjob.future
            self._set(req, COMPLETED)
            return result
        except asyncio.CancelledError:
            self._set(req, CANCELLED)
            raise
        except Exception as exc:
            req.error = str(exc)
            self._set(req, FAILED)
            raise
        finally:
            with self._lock:
                self._active.pop(req.id, None)
                self.history.append(req)

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            found = self._active.get(request_id)
        if not found:
            return False
        return (self.sched or R.scheduler()).cancel(found[1])


_GATEWAY: Gateway | None = None


def gateway() -> Gateway:
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = Gateway()
    return _GATEWAY
