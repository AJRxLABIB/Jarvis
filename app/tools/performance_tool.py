"""``performance_mode``: see or change how hard JARVIS works - Fast, Balanced, High quality, Battery saver or Auto - with the MEASURED
numbers behind it, and ask "will this model fit?". Rules live in app/core/perf.py."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core import perf
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class PerfArgs(ToolArgs):
    action: Literal["status", "set", "fit"] = Field(
        default="status", description="status = the mode in force, why, and the measured resources; set = change the mode; "
                                      "fit = will an installed model fit right now (needs model)")
    mode: str = Field(default="", max_length=20, description="For set: auto, fast, balanced, quality (high quality) or battery (battery saver)")
    model: str = Field(default="", max_length=80, description="For fit: the model name (installed in Ollama), or empty for the main model")


def _snapshot_words(s: perf.Snapshot) -> str:
    bits = []
    if s.ram_free_gb is not None:
        bits.append(f"memory {s.ram_free_gb:.1f} of {s.ram_total_gb:.1f} GB free")
    if s.cpu_pct is not None:
        bits.append(f"CPU {s.cpu_pct:.0f}%")
    if s.vram_total_gb is not None:
        bits.append(f"GPU memory {s.vram_free_gb:.1f} of {s.vram_total_gb:.1f} GB free")
    else:
        bits.append("GPU memory not read (no NVIDIA GPU answered)")
    if s.battery_pct is not None:
        bits.append(f"battery {s.battery_pct:.0f}%" + (" (on battery)" if s.on_battery else " (plugged in)"))
    if s.disk_free_gb is not None:
        bits.append(f"disk {s.disk_free_gb:.0f} GB free")
    return ", ".join(bits)


async def performance_mode(args: PerfArgs, ctx: ToolContext) -> ToolResult:
    import asyncio

    snap = await asyncio.to_thread(perf.read_snapshot)
    if args.action == "set":
        wanted = perf.normalise(args.mode)
        if not args.mode.strip() or (wanted == "balanced" and args.mode.strip().lower() not in ("balanced", "normal", "default")):
            raise ToolError("Which mode: auto, fast, balanced, high quality or battery saver?", "bad_mode")
        if ctx.apply_setting is None:
            raise ToolError("Settings can only be changed while the Jarvis window is open.", "unavailable")
        ctx.apply_setting("performance_mode", wanted)
        plan = perf.plan(wanted, snap)
        return ToolResult.success(f"Performance mode: {plan.describe()}", mode=plan.mode, requested=wanted)
    if args.action == "fit":
        ai = ctx.ai
        if ai is None:
            raise ToolError("No model service is connected.", "unavailable")
        models = await ai.models()
        name = args.model.strip().lower()
        if name:
            found = next((m for m in models if m.name.lower() == name or m.name.lower().startswith(name)), None)
        else:
            found, _ = await ai.resolve_model()
        if found is None:
            raise ToolError(f"'{args.model}' is not installed in Ollama (installed: {', '.join(m.name for m in models) or 'none'}).", "not_found")
        gb = found.size_bytes / 1e9 if found.size_bytes else None
        ok, why = perf.fit_check(gb, snap)
        return ToolResult.success(f"{found.name}: {why}.", model=found.name, fits=ok, size_gb=gb)
    plan = perf.plan(getattr(ctx.settings, "performance_mode", "balanced"), snap)
    return ToolResult.success(f"{plan.describe()}. Measured now: {_snapshot_words(snap)}.", mode=plan.mode, requested=plan.requested,
                              route_shift=plan.route_shift, defer_background=plan.defer_background)


def _risk(args: PerfArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.SAFE


TOOLS: tuple[Tool, ...] = (
    Tool("performance_mode", "Performance mode: Fast, Balanced, High quality, Battery saver or Auto. Shows the mode in force with measured "
         "memory / CPU / GPU / battery, changes it, or checks whether an installed model fits right now.",
         performance_mode, PerfArgs, RiskLevel.SAFE, "system", timeout=20, risk=_risk),
)
