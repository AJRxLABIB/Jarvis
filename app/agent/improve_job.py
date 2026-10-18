"""Runs the self-improvement test against the real local models (Ollama). Used by ``python run.py --self-improve``
and the ``improve_yourself`` tool. The decision logic lives in app/agent/self_improve.py (tested without Ollama)."""

from __future__ import annotations

from datetime import datetime

from app.agent import self_improve
from app.agent.growth import Growth


def _ask_factory(settings, registry):
    from app.agent.tool_router import select_specs
    from app.ai.base import ChatMessage, ChatRequest
    from app.ai.manager import create_provider
    from app.ai.prompts import build_system_prompt

    providers = {}

    async def ask(model: str, temperature: float, text: str):
        provider = providers.get(temperature)
        if provider is None:
            provider = providers[temperature] = create_provider(settings.model_copy(update={"ai_temperature": temperature}))
        specs = select_specs(registry, text)
        system = build_system_prompt(assistant_name=settings.assistant_name, now=datetime.now(), tools=specs,
                                     user_address=settings.user_address)
        response = await provider.chat(ChatRequest(model=model, messages=(ChatMessage.system(system),
                                                                         ChatMessage.user(text)), tools=tuple(specs)))
        calls = response.message.tool_calls
        if not calls:
            return None, False
        call = calls[0]
        tool = registry.get(call.name)
        if tool is None:
            return call.name, True                                  # a tool that doesn't exist: malformed
        try:
            tool.args_model.model_validate(call.arguments or {})
        except Exception:
            return call.name, True                                  # wrong / missing arguments
        return call.name, False

    return ask, providers


async def improve_now(settings, paths, database, registry, *, apply_changes: bool = True):
    from app.ai import jarvis_model
    from app.ai.health import total_memory_bytes
    from app.ai.manager import create_provider
    from app.memory.settings_store import SettingsStore

    cases = Growth(database).cases()
    provider = create_provider(settings)
    models = await provider.list_models()
    store = SettingsStore(database)
    current_base = store.get(jarvis_model.BASE_KEY) or settings.ai_model or (
        jarvis_model.choose_base(models).name if jarvis_model.choose_base(models) else "")
    if not current_base:
        return self_improve.Outcome(False, "No chat model is installed to improve.")
    options = self_improve.candidates(current_base, float(settings.ai_temperature), models, total_memory_bytes())
    ask, providers = _ask_factory(settings, registry)

    async def apply(candidate: self_improve.Candidate) -> None:
        if not apply_changes:
            return
        from app.config.env_writer import update_env_file

        params = {**jarvis_model.parameters(settings), "temperature": candidate.temperature}
        await provider.create(jarvis_model.JARVIS_MODEL, candidate.base, jarvis_model.SYSTEM, params)
        store.set(jarvis_model.BASE_KEY, candidate.base)
        update_env_file(paths.env_file, {"AI_MODEL": jarvis_model.JARVIS_MODEL,
                                         "AI_TEMPERATURE": str(candidate.temperature)})

    try:
        return await self_improve.run(ask, apply, cases, options, paths.data_dir)
    finally:
        for p in (provider, *providers.values()):
            try:
                await p.aclose()
            except Exception:
                pass
