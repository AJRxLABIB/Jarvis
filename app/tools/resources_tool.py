"""``resources``: GPU / CPU / RAM, the models Ollama has loaded (with measured memory), whether a model would fit, and the current performance plan (core/resources.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core import perf, resources as R
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolResult


class ResourceArgs(ToolArgs):
    action: Literal["status", "models", "fit"] = Field(default="status", description="status = GPU / CPU / RAM and the performance plan; models = loaded models and the largest memory seen for each; "
                                                                                      "fit = would a model of model_gb fit right now")
    model_gb: float | None = Field(default=None, ge=0.1, le=200.0, description="For fit: the model's size in GB")
    model: str = Field(default="", max_length=80, description="For fit: the model's name, to use the memory measured when it was loaded before")


async def resources(args: ResourceArgs, ctx: ToolContext) -> ToolResult:
    import asyncio

    gpu = await asyncio.to_thread(R.read_gpu)
    snap = await asyncio.to_thread(perf.read_snapshot)
    loaded: list[R.Loaded] = []
    provider = getattr(getattr(ctx, "ai", None), "provider", None)
    if provider is not None and hasattr(provider, "_request"):
        try:
            loaded = R.parse_ps(await provider._request("GET", "/api/ps"))
        except Exception:
            loaded = []
    book = None
    if ctx.database is not None:
        from app.memory.settings_store import SettingsStore

        book = R.ModelBook(SettingsStore(ctx.database))
        if loaded:
            book.observe(loaded)
    if args.action == "models":
        hist = book.history() if book else {}
        lines = [f"{m.name}: {m.size_gb:.1f} GB loaded, {m.gpu_share:.0%} in GPU memory" for m in loaded] or ["No model is loaded right now."]
        lines += [f"seen before: {n} up to {r['total_gb']:.1f} GB ({r['vram_gb']:.1f} GB in GPU memory)" for n, r in hist.items() if n not in {m.name for m in loaded}][:8]
        return ToolResult.success("\n".join(lines), loaded=[m.name for m in loaded])
    if args.action == "fit":
        plan = R.plan_load(args.model_gb, gpu, snap.ram_free_gb, book.observed_gb(args.model) if book and args.model else None)
        return ToolResult.success(f"{plan.verdict.upper()}: {plan.why}.", verdict=plan.verdict)
    g = (f"GPU {gpu.name}: {gpu.used_gb:.1f}/{gpu.total_gb:.1f} GB used, {gpu.util_pct:.0f}% busy" + (f", {gpu.temp_c:.0f} C" if gpu.temp_c is not None else "")
         + f" (read via {gpu.source})") if gpu.total_gb is not None else "No NVIDIA GPU was read (CPU only, or nvidia-smi / NVML unavailable)"
    cpu = f"CPU {snap.cpu_pct:.0f}%, RAM {snap.ram_free_gb:.1f} of {snap.ram_total_gb:.1f} GB free" if snap.ram_total_gb else "CPU / RAM not read"
    models = ("; loaded: " + ", ".join(f"{m.name} ({m.gpu_share:.0%} on GPU)" for m in loaded)) if loaded else "; no model loaded"
    plan = perf.plan(getattr(ctx.settings, "performance_mode", "balanced"), snap)
    return ToolResult.success(f"{g}. {cpu}{models}. Mode: {plan.describe()}", gpu_source=gpu.source, loaded=[m.name for m in loaded])


TOOLS: tuple[Tool, ...] = (
    Tool("resources", "Show GPU memory, CPU, RAM, the AI models currently loaded and whether a model would fit ('how much GPU memory is free', 'what models are loaded', "
         "'will this model fit').", resources, ResourceArgs, RiskLevel.SAFE, "windows", timeout=25),
    )
