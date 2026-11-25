"""One model called "jarvis": no more guessing which installed model is the right one.

"Make the Jarvis model" (Menu, or jarvis-cli --make-jarvis-model) creates the Ollama model
"jarvis" from the best installed chat model (AI_MODEL, else qwen3:4b-instruct, else the largest
one that fits), with Jarvis's settings built in, and switches Jarvis to it. The weights are
shared, so it takes seconds and no extra disk space. Run it again after installing a better
model (jarvis-cli --make-jarvis-model --model qwen3:8b): "jarvis" is replaced by the new one.

Afterwards the other chat models can be deleted (you are shown the list and asked first).
Kept: "jarvis" itself, the embedding model (search by meaning), the vision model (screen), and any
AI_FAST_MODEL / AI_SMART_MODEL you set - they do different jobs.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.base import ModelInfo

JARVIS_MODEL = "jarvis"
PREFERRED_BASES = ("qwen3:4b-instruct", "qwen3:8b", "qwen2.5:7b", "llama3.1:8b", "qwen3:4b", "llama3.2:3b")
BASE_KEY = "jarvis_model_base"
SYSTEM = ("You are Jarvis, a warm, capable personal assistant on the user's Windows PC. You understand Bangla and "
          "English and answer in the language the user used. Be brief and natural.")


def _same(a: str, b: str) -> bool:
    norm = lambda n: n.lower() if ":" in n else f"{n.lower()}:latest"  # noqa: E731
    return norm(a) == norm(b)


def is_jarvis(name: str) -> bool:
    return _same(name, JARVIS_MODEL)


def choose_base(models: list[ModelInfo], current: str = "") -> ModelInfo | None:
    """The model to build "jarvis" from: the configured one, a known good one, else the biggest chat model."""
    chat = [m for m in models if m.can_chat and not is_jarvis(m.name) and not m.is_vision_only]
    if not chat:
        return None
    for wanted in (current, *PREFERRED_BASES):
        if wanted and not is_jarvis(wanted):
            found = next((m for m in chat if _same(m.name, wanted)), None)
            if found is not None:
                return found
    return max(chat, key=lambda m: m.size_bytes or 0)


@dataclass(frozen=True)
class Cleanup:
    delete: list[str]
    keep: list[str]


def cleanup_plan(models: list[ModelInfo], *, keep_names: tuple[str, ...] = ()) -> Cleanup:
    """Which models can go once "jarvis" exists: other chat models, but never embedding or vision models or
    the ones named in ``keep_names`` (AI_FAST_MODEL, AI_SMART_MODEL, VISION_MODEL, EMBEDDING_MODEL)."""
    delete, keep = [], []
    for model in models:
        protected = (is_jarvis(model.name) or not model.can_chat or model.supports_vision
                     or any(name and _same(model.name, name) for name in keep_names))
        (keep if protected else delete).append(model.name)
    return Cleanup(delete, keep)


def parameters(settings) -> dict:
    return {"num_ctx": int(settings.ai_context_window), "temperature": float(settings.ai_temperature)}


async def build(ai, base: str, settings) -> str:
    """Create (or replace) "jarvis" from ``base`` and switch to it. Returns the model name."""
    await ai.provider.create(JARVIS_MODEL, base, SYSTEM, parameters(settings))
    if getattr(ai, "store", None) is not None:
        ai.store.set(BASE_KEY, base)
    await ai.refresh_models()
    await ai.set_model(JARVIS_MODEL)
    return JARVIS_MODEL


async def delete_models(ai, names: list[str]) -> list[str]:
    """Delete these models; returns the ones that were removed (never "jarvis")."""
    removed = []
    for name in names:
        if is_jarvis(name):
            continue
        await ai.provider.delete(name)
        removed.append(name)
    await ai.refresh_models()
    return removed
