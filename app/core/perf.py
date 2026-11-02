"""Performance modes - Fast, Balanced, High quality, Battery saver (or Auto) - decided from MEASURED resources, never guessed.

``read_snapshot`` measures RAM, CPU, battery, free disk and (NVIDIA only) GPU memory; anything it cannot read stays ``None`` and is
never reported as zero. ``plan`` turns a mode + a snapshot into concrete limits that the existing pieces obey:

* the model router moves one step toward a smaller / larger model role (``shift_route``) - only among roles you configured;
* quiet background chores (the daily backup after its first week, watched research) wait while the PC is busy or on a low battery;
* hand-gesture capture is capped (frames per second, hands) so the camera never starves the voice and typing paths;
* ``fit_check`` explains, in numbers, why a model will not fit and what to do (smaller model / CPU offload / close something).

Pure functions over a ``Snapshot``: tested with made-up numbers; only ``read_snapshot`` touches the machine.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

MODES = ("fast", "balanced", "quality", "battery")
LABELS = {"fast": "Fast", "balanced": "Balanced", "quality": "High quality", "battery": "Battery saver", "auto": "Auto"}
LOW_BATTERY = 30           # % on battery at which Auto switches to Battery saver
BUSY_CPU = 85.0            # % CPU above which quiet chores wait
LOW_RAM_GB = 2.0           # free RAM below which Auto prefers Fast
HEADROOM = 1.15            # a model needs about its file size plus working memory (same factor as app/ai/health.py)
ORDER = ("fast", "normal", "smart")         # router roles from smallest to largest


@dataclass(frozen=True)
class Snapshot:
    ram_total_gb: float | None = None
    ram_free_gb: float | None = None
    cpu_pct: float | None = None
    battery_pct: float | None = None
    on_battery: bool | None = None
    disk_free_gb: float | None = None
    vram_total_gb: float | None = None
    vram_free_gb: float | None = None
    at: float = field(default=0.0, compare=False)


@dataclass(frozen=True)
class Plan:
    mode: str                       # what is in force (never "auto")
    requested: str                  # what the setting says
    why: str
    route_shift: int                # -1 prefer a smaller model role, +1 a larger one
    allow_thinking: bool
    defer_background: bool
    gesture_fps: float
    gesture_hands: int
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        extra = f" ({self.why})" if self.why else ""
        return f"{LABELS[self.mode]}{extra}: " + ("; ".join(self.notes) if self.notes else "no limits applied")


def normalise(mode: str | None) -> str:
    m = (mode or "balanced").strip().lower().replace("-", "_").replace(" ", "_")
    return {"high_quality": "quality", "battery_saver": "battery", "saver": "battery", "best": "quality", "speed": "fast"}.get(m, m) \
        if m in MODES or m in ("auto", "high_quality", "battery_saver", "saver", "best", "speed") else "balanced"


def effective(requested: str, snap: Snapshot) -> tuple[str, str]:
    """The mode actually used and why. Only ``auto`` looks at the snapshot; an explicit choice is always honoured."""
    requested = normalise(requested)
    if requested != "auto":
        return requested, ""
    if snap.on_battery and snap.battery_pct is not None and snap.battery_pct <= LOW_BATTERY:
        return "battery", f"on battery at {snap.battery_pct:.0f}%"
    if snap.ram_free_gb is not None and snap.ram_free_gb < LOW_RAM_GB:
        return "fast", f"only {snap.ram_free_gb:.1f} GB of memory is free"
    if snap.cpu_pct is not None and snap.cpu_pct >= BUSY_CPU:
        return "fast", f"the CPU is {snap.cpu_pct:.0f}% busy"
    return "balanced", "plugged in, nothing is straining the PC" if snap.on_battery is False else ""


def plan(requested: str, snap: Snapshot) -> Plan:
    mode, why = effective(requested, snap)
    busy = snap.cpu_pct is not None and snap.cpu_pct >= BUSY_CPU
    low_disk = snap.disk_free_gb is not None and snap.disk_free_gb < 2.0
    shift = {"fast": -1, "battery": -1, "balanced": 0, "quality": 1}[mode]
    notes = []
    if shift < 0:
        notes.append("answers use the smaller model where one is set")
    if shift > 0:
        notes.append("answers use the larger model where one is set")
    allow_thinking = mode == "quality" or (mode == "balanced" and not busy)
    if not allow_thinking:
        notes.append("slow 'thinking' mode is not started automatically")
    defer = mode == "battery" or busy or (mode == "fast" and busy)
    if defer:
        notes.append("background chores wait")
    fps, hands = {"fast": (30.0, 2), "balanced": (30.0, 2), "quality": (30.0, 2), "battery": (15.0, 1)}[mode]
    if busy and mode != "quality":
        fps = min(fps, 20.0)
    if fps < 30:
        notes.append(f"hand tracking is limited to {fps:g} frames/s" + (", one hand" if hands == 1 else ""))
    if low_disk:
        notes.append(f"only {snap.disk_free_gb:.1f} GB of disk is free")
    return Plan(mode, normalise(requested), why, shift, allow_thinking, defer, fps, hands, tuple(notes))


def shift_route(kind: str, p: Plan, configured: set[str]) -> tuple[str, str]:
    """Move a routed role one step by the plan, only to a role that is configured. Coding stays coding. -> (kind, note)."""
    if p.route_shift == 0 or kind not in ORDER:
        return kind, ""
    i = ORDER.index(kind) + p.route_shift
    if not 0 <= i < len(ORDER):
        return kind, ""
    target = ORDER[i]
    if target not in configured and target != "normal":         # "normal" (AI_MODEL) always exists
        return kind, ""
    return target, f"{LABELS[p.mode]} mode"


def fit_check(model_gb: float | None, snap: Snapshot) -> tuple[bool | None, str]:
    """Will a model of this size load? (True/False/None = cannot tell) and a plain explanation with numbers and a way out."""
    if not model_gb:
        return None, "the model's size is unknown, so I cannot say whether it fits"
    need = model_gb * HEADROOM
    if snap.vram_total_gb is not None and snap.vram_free_gb is not None:
        if need <= snap.vram_free_gb:
            return True, f"fits in GPU memory ({need:.1f} GB needed, {snap.vram_free_gb:.1f} GB free)"
        if snap.ram_free_gb is not None and need <= snap.ram_free_gb:
            return True, (f"too big for the GPU right now ({need:.1f} GB needed, {snap.vram_free_gb:.1f} GB free) - it can run mostly on the "
                          f"CPU in system memory ({snap.ram_free_gb:.1f} GB free), slower")
        return False, (f"needs about {need:.1f} GB; GPU has {snap.vram_free_gb:.1f} GB free and memory {snap.ram_free_gb or 0:.1f} GB free. "
                       "Close programs that use memory, or choose a smaller model")
    if snap.ram_free_gb is not None:
        if need <= snap.ram_free_gb:
            return True, f"fits in memory ({need:.1f} GB needed, {snap.ram_free_gb:.1f} GB free); no NVIDIA GPU was read, so it runs on the CPU"
        return False, f"needs about {need:.1f} GB but only {snap.ram_free_gb:.1f} GB of memory is free. Close programs or choose a smaller model"
    return None, "free memory could not be read, so I cannot say whether it fits"


# ---------------------------------------------------------------------------------------------------- measuring
_CACHE: tuple[float, Snapshot] | None = None


def read_snapshot(*, max_age: float = 20.0, reader: Any = None, now: float | None = None) -> Snapshot:
    """Measure (cached for ``max_age`` seconds - the GPU query starts a process). ``reader`` replaces the machine in tests."""
    global _CACHE
    t = time.monotonic() if now is None else now
    if reader is None and _CACHE is not None and t - _CACHE[0] < max_age:
        return _CACHE[1]
    snap = (reader or _read_machine)()
    if reader is None:
        _CACHE = (t, snap)
    return snap


def _read_machine() -> Snapshot:
    vals: dict[str, Any] = {"at": time.time()}
    try:
        import psutil

        vm = psutil.virtual_memory()
        vals.update(ram_total_gb=vm.total / 1e9, ram_free_gb=vm.available / 1e9, cpu_pct=psutil.cpu_percent(interval=None))
        battery = psutil.sensors_battery()
        if battery is not None:
            vals.update(battery_pct=float(battery.percent), on_battery=not battery.power_plugged)
        import shutil

        vals["disk_free_gb"] = shutil.disk_usage(".").free / 1e9
    except Exception:
        pass
    try:
        from app.tools.system import gpu_info

        gpus = gpu_info().get("gpus") or []
        if gpus:
            best = max(gpus, key=lambda g: float(g["memory_total_mb"]))
            total, used = float(best["memory_total_mb"]), float(best["memory_used_mb"])
            vals.update(vram_total_gb=total / 1024, vram_free_gb=max(0.0, total - used) / 1024)
    except Exception:
        pass
    return Snapshot(**vals)


def current_plan(settings: Any, *, reader: Any = None) -> Plan:
    return plan(getattr(settings, "performance_mode", "balanced"), read_snapshot(reader=reader))
