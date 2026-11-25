"""``inference_providers``: which local AI engines are configured (Ollama, Foundry Local), whether each answers, and the models each offers - from what they report."""

from __future__ import annotations

import asyncio

from app.ai.base import AIError
from app.ai.foundry_local import FoundryLocalProvider
from app.ai.ollama import OllamaProvider
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolContext, ToolResult


async def _probe(provider) -> str:
    try:
        await asyncio.wait_for(provider.health(), 5)
        models = await asyncio.wait_for(provider.list_models(), 10)
    except (AIError, asyncio.TimeoutError) as exc:
        return f"not available ({str(exc) or 'timed out'})"
    except Exception as exc:
        return f"error ({exc.__class__.__name__})"
    finally:
        await provider.aclose()
    names = ", ".join(m.name + (" [vision]" if m.supports_vision else "") for m in models[:8]) or "no models"
    return f"answering, {len(models)} model(s): {names}" + (" ..." if len(models) > 8 else "")


async def inference_providers(_: NoArgs, ctx: ToolContext) -> ToolResult:
    s = ctx.settings
    lines = [f"Active provider: {s.ai_provider}."]
    lines.append("Ollama at " + s.ollama_host + ": " + await _probe(OllamaProvider(s.ollama_host, request_timeout=8.0, offline_mode=s.forced_offline)))
    if s.foundry_local_url:
        try:
            lines.append("Foundry Local at " + s.foundry_local_url + ": " + await _probe(FoundryLocalProvider(s.foundry_local_url, request_timeout=8.0)))
        except AIError as exc:
            lines.append(f"Foundry Local: {exc}")
    else:
        lines.append("Foundry Local: not configured (optional; set FOUNDRY_LOCAL_URL from 'foundry service status').")
    lines.append("Capabilities (vision, tools) are shown only when an engine reports them; Foundry Local reports none, so none are assumed.")
    return ToolResult.success("\n".join(lines))


async def model_registry(_: NoArgs, ctx: ToolContext) -> ToolResult:
    from app.ai import modelbench
    from app.core import model_registry as MR

    s = ctx.settings
    provider = OllamaProvider(s.ollama_host, request_timeout=8.0, offline_mode=s.forced_offline)
    try:
        models = await asyncio.wait_for(provider.list_models(), 10)
        details = {}
        for m in models[:30]:
            try:
                details[m.name] = await provider.model_details(m.name)
            except Exception:
                continue
    except (AIError, asyncio.TimeoutError) as exc:
        return ToolResult.failure(f"Could not read the model list: {str(exc) or 'timed out'}")
    finally:
        await provider.aclose()
    speed = modelbench.load_history(ctx.paths.data_dir if ctx.paths is not None else None)
    entries = MR.build(models, speed, details)
    return ToolResult.success(MR.report(entries), models=len(entries))


TOOLS: tuple[Tool, ...] = (
    Tool("model_registry", "List every installed local AI model with its family, size, quantization, reported capabilities, context length, licence text and measured speed ('what models do I have', 'which models support tools', 'model licences').",
         model_registry, NoArgs, RiskLevel.SAFE, "info", timeout=60),
    Tool("inference_providers", "Show which local AI engines are set up (Ollama, Foundry Local), whether each is answering and which models they offer ('which AI engines do I have').",
         inference_providers, NoArgs, RiskLevel.SAFE, "info", timeout=30),
)
