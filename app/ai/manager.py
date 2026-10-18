"""Model selection and a single entry point for AI requests.

Model resolution order:
1. the model chosen during this session (``set_model``)
2. ``AI_MODEL`` from .env / environment
3. the model last saved from the app (settings store)
4. automatic: a chat model with tool support, most recently installed first
An explicitly requested model that is not installed is an error; the manager
never silently switches to another model.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace

import httpx

from app.ai.base import (
    AIError,
    AIProvider,
    AIRequestError,
    AITimeoutError,
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ModelCapabilityError,
    ModelInfo,
    ModelNotFoundError,
    NoModelAvailableError,
    ProviderUnavailableError,
    ToolSpec,
)
from app.ai.health import ModelHealth, could_ever_fit, total_memory_bytes
from app.ai.ollama import OllamaProvider
from app.config.settings import Settings
from app.memory.settings_store import SettingsStore
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("ai")
SAVED_MODEL_KEY = "ai.model"
NO_MODEL_MESSAGE = (
    "No chat model is installed in Ollama. Install one with 'ollama pull <model-name>' and try again."
)

ProviderFactory = Callable[[Settings, "httpx.AsyncBaseTransport | None"], AIProvider]


def _ollama_factory(settings: Settings, transport: httpx.AsyncBaseTransport | None) -> AIProvider:
    return OllamaProvider(
        settings.ollama_host,
        request_timeout=settings.ai_request_timeout,
        keep_alive=settings.ollama_keep_alive,
        context_window=settings.ai_context_window,
        temperature=settings.ai_temperature,
        offline_mode=settings.forced_offline,
        transport=transport,
    )


# Register new providers here (the name must also be in SUPPORTED_AI_PROVIDERS).
PROVIDERS: dict[str, ProviderFactory] = {"ollama": _ollama_factory}


def create_provider(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> AIProvider:
    try:
        factory = PROVIDERS[settings.ai_provider]
    except KeyError:
        raise ProviderUnavailableError(f"The AI provider '{settings.ai_provider}' is not available.") from None
    return factory(settings, transport)


def _normalize(name: str) -> str:
    name = name.strip().lower()
    return name if ":" in name else f"{name}:latest"


def find_model(models: Sequence[ModelInfo], name: str) -> ModelInfo | None:
    """Exact match (``llama3.2`` == ``llama3.2:latest``); a bare name without a
    tag also matches when exactly one installed model has that base name."""
    wanted = _normalize(name)
    for model in models:
        if _normalize(model.name) == wanted:
            return model
    if ":" not in name:
        base = name.strip().lower()
        same_base = [m for m in models if m.name.lower().split(":", 1)[0] == base]
        if len(same_base) == 1:
            return same_base[0]
    return None


_VISION_HINTS = ("llava", "vision", "-vl", "vl:", "gemma3", "minicpm-v", "moondream", "llama4", "qwen2.5vl",
                 "qwen3-vl", "mistral-small3")


def can_see_images(model: ModelInfo) -> bool:
    """Reported 'vision' capability; for older Ollama versions, a guess from well-known names."""
    if model.capabilities is not None:
        return model.supports_vision
    name = model.name.lower()
    return any(hint in name for hint in _VISION_HINTS)


@dataclass(frozen=True)
class AIStatus:
    provider: str
    reachable: bool
    version: str | None = None
    models: tuple[ModelInfo, ...] = ()
    selected: ModelInfo | None = None
    selection_source: str | None = None  # session | env | saved | auto
    message: str | None = None  # user-facing problem description


@dataclass
class AIManager:
    settings: Settings
    provider: AIProvider
    store: SettingsStore | None = None
    _session_model: str | None = field(default=None, init=False)
    _models: list[ModelInfo] | None = field(default=None, init=False)
    health: ModelHealth = field(default_factory=ModelHealth, init=False)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        store: SettingsStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "AIManager":
        return cls(settings=settings, provider=create_provider(settings, transport), store=store)

    # -------------------------------------------------------------------- models

    async def _capabilities(self, name: str) -> frozenset[str] | None:
        try:
            return await self.provider.model_capabilities(name)
        except (ProviderUnavailableError, asyncio.CancelledError):
            raise
        except AIError:
            return None

    async def refresh_models(self) -> list[ModelInfo]:
        models = await self.provider.list_models()
        capabilities = await asyncio.gather(*(self._capabilities(m.name) for m in models))
        self._models = [replace(m, capabilities=c) for m, c in zip(models, capabilities)]
        return list(self._models)

    async def models(self) -> list[ModelInfo]:
        return list(self._models) if self._models is not None else await self.refresh_models()

    def _saved_model(self) -> str | None:
        if self.store is None:
            return None
        try:
            value = self.store.get(SAVED_MODEL_KEY)
        except Exception:  # a broken store must not stop the assistant
            _log.warning("Could not read saved model", exc_info=True)
            return None
        return value if isinstance(value, str) and value else None

    async def resolve_model(self) -> tuple[ModelInfo, str]:
        models = await self.models()
        chat_models = [m for m in models if m.can_chat]
        if not chat_models:
            raise NoModelAvailableError(NO_MODEL_MESSAGE)

        for source, name in (
            ("session", self._session_model),
            ("env", self.settings.ai_model or None),
            ("saved", self._saved_model()),
        ):
            if not name:
                continue
            model = find_model(chat_models, name)
            if model is not None:
                return model, source
            if source == "saved":
                log_event(_log, "saved_model_missing", model=name)
                self.__dict__["_saved_missing"] = name           # told once with the next answer (see resolve notice)
                continue
            installed = ", ".join(m.name for m in chat_models)
            origin = "AI_MODEL in .env" if source == "env" else "the selected model"
            raise ModelNotFoundError(
                f"The model '{name}' ({origin}) is not installed in Ollama. Installed: {installed}."
            )

        ranked = sorted(chat_models, key=lambda m: (m.supports_tools is True, m.modified_at or ""), reverse=True)
        return ranked[0], "auto"

    async def set_model(self, name: str, *, persist: bool = True) -> ModelInfo:
        models = await self.refresh_models()
        model = find_model(models, name)
        if model is None:
            installed = ", ".join(m.name for m in models if m.can_chat) or "none"
            raise ModelNotFoundError(f"The model '{name}' is not installed in Ollama. Installed: {installed}.")
        if not model.can_chat:
            raise ModelCapabilityError(f"'{model.name}' is an embedding model and cannot chat.")
        self._session_model = model.name
        if persist and self.store is not None:
            self.store.set(SAVED_MODEL_KEY, model.name)
        log_event(_log, "model_selected", model=model.name, persisted=persist)
        return model

    # ------------------------------------------------------------------- status

    async def status(self) -> AIStatus:
        name = self.provider.name
        try:
            health = await self.provider.health()
        except AIError as exc:
            return AIStatus(provider=name, reachable=False, message=str(exc))
        try:
            models = await self.refresh_models()
            selected, source = await self.resolve_model()
        except AIError as exc:
            return AIStatus(
                provider=name, reachable=True, version=health.version,
                models=tuple(self._models or ()), message=str(exc),
            )
        return AIStatus(
            provider=name, reachable=True, version=health.version, models=tuple(models),
            selected=selected, selection_source=source,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embeddings from the local embedding model (EMBEDDING_MODEL)."""
        name = self.settings.embedding_model
        embed = getattr(self.provider, "embed", None)
        if embed is None:
            raise ModelCapabilityError("This AI provider can't create embeddings.")
        try:
            return await embed(name, texts)
        except ModelNotFoundError as exc:
            raise ModelNotFoundError(f"The embedding model '{name}' is not installed. Run: ollama pull {name}") from exc

    gpu_share: float | None = None  # how much of the model sits in GPU memory after the last warm-up
    FIT_STEPS = (6144, 4096)  # context sizes tried when the model doesn't fit on the GPU (tokens)
    FULLY_ON_GPU = 0.97

    async def warm_up(self) -> str | None:
        """Load the selected model now. Returns its name, or None if that wasn't possible (never raises).
        Then check it is entirely in GPU memory; if not, use a smaller context until it is (a model partly
        on the CPU answers 5-10x slower)."""
        try:
            model, _ = await self.resolve_model()
            started = time.perf_counter()
            await self.provider.warm_up(model.name)
        except Exception as exc:  # warming up is an optimisation only
            log_event(_log, "model_warm_up_failed", detail=str(exc)[:200])
            return None
        log_event(_log, "model_warmed_up", model=model.name, ms=int((time.perf_counter() - started) * 1000))
        await self._fit_on_gpu(model.name)
        return model.name

    async def _fit_on_gpu(self, model: str) -> None:
        share_of = getattr(self.provider, "gpu_share", None)
        if share_of is None:
            return
        try:
            share = await share_of(model)
        except Exception:
            return
        self.gpu_share = share
        if share is None:
            return
        log_event(_log, "model_gpu_share", model=model, share=round(share, 3),
                  context=getattr(self.provider, "context_window", None))
        if share >= self.FULLY_ON_GPU or share == 0.0:  # 0: no GPU used at all (a CPU-only PC): nothing to fit
            return
        for size in self.FIT_STEPS:
            if size >= getattr(self.provider, "context_window", 0):
                continue
            self.provider.context_window = size
            try:
                await self.provider.unload(model)  # load it again at the smaller size
                await self.provider.warm_up(model)
                share = await share_of(model)
            except Exception:
                return
            self.gpu_share = share
            log_event(_log, "context_lowered_to_fit", model=model, context=size,
                      share=None if share is None else round(share, 3))
            if share is None or share >= self.FULLY_ON_GPU:
                return

    # --------------------------------------------------------------------- chat

    async def _fast_model(self) -> ModelInfo | None:
        """AI_FAST_MODEL, if it is installed (and can use tools)."""
        return await self._optional_model(self.settings.ai_fast_model, "fast")

    async def _optional_model(self, name: str, kind: str) -> ModelInfo | None:
        if not name:
            return None
        model = find_model([m for m in await self.models() if m.can_chat], name)
        if model is None:
            log_event(_log, f"{kind}_model_missing", model=name)
        return model

    async def _prepare(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec],
                       fast: bool = False, smart: bool = False, coding: bool = False,
                       sensitive: bool = False, untrusted: bool = False) -> ChatRequest:
        if sensitive:
            from app.ai.router import is_local_host

            if not is_local_host(self.settings.ollama_host):
                raise AIError("This request looks private, and your AI model runs on a computer on the internet "
                              f"({self.settings.ollama_host}). I won't send it there; use an Ollama on this PC or "
                              "your own network.")
        model, role = None, "default"
        if coding:  # AI_CODING_MODEL, else the smart model
            model = await self._optional_model(getattr(self.settings, "ai_coding_model", ""), "coding")
            role = "coding"
            if model is None:
                smart = True
        if model is None and smart:  # AI_SMART_MODEL for hard questions
            model = await self._optional_model(self.settings.ai_smart_model, "smart")
            role = "smart"
        elif model is None and fast:
            model = await self._fast_model()
            role = "fast"
        substituted = None                     # why the chosen role model isn't used (shown to you once, never silent)
        if model is not None and not self.health.available(model.name):
            log_event(_log, "model_skipped_unhealthy", model=model.name, seconds=round(self.health.cooldown_left(model.name)))
            substituted = (model.name, "kept failing, so it's resting for a few minutes")
            model = None  # it keeps failing: use the default model until its cool-off ends
        if model is not None and not could_ever_fit(model.size_bytes, total_memory_bytes()):
            log_event(_log, "model_too_big_for_this_pc", model=model.name, size=model.size_bytes)
            substituted = (model.name, "is too big for this PC's memory")
            model = None  # bigger than this PC's RAM: it could never load
        if model is not None and tools and model.supports_tools is False:
            substituted = (model.name, "can't use tools")
        if model is None or (tools and model.supports_tools is False):
            model, _ = await self.resolve_model()
            role = "default"
        self.last_route = {"role": role, "model": model.name, "fallback": None}
        if substituted is not None and substituted[0] != model.name:
            self.last_route["fallback"] = self._announce_once(f"{substituted[0]} {substituted[1]}")
        missing = self.__dict__.pop("_saved_missing", None)
        if missing and not self.last_route.get("fallback"):
            self.last_route["fallback"] = self._announce_once(f"your selected model {missing} is no longer installed")
        if getattr(self.settings, "ai_auto_switch", False):
            model = await self._auto_switch(model, tools, untrusted)
        if tools and model.supports_tools is False:
            raise ModelCapabilityError(
                f"The model '{model.name}' does not support tool calling. Choose a model that does."
            )
        think = self.settings.ai_think if model.supports_thinking else None
        return ChatRequest(model=model.name, messages=tuple(messages), tools=tuple(tools), think=think)

    async def _auto_switch(self, model: ModelInfo, tools: Sequence[ToolSpec], untrusted: bool) -> ModelInfo:
        """AI_AUTO_SWITCH: replace ``model`` by an installed, benchmark-backed one when app/ai/switching.py says so."""
        from app.ai import switching

        installed = [m for m in await self.models() if m.can_chat and (m.supports_tools is not False or not tools)
                     and self.health.available(m.name) and could_ever_fit(m.size_bytes, total_memory_bytes())]
        pick = switching.decide(model.name, [m.name for m in installed],
                                switching.load_evidence(self.settings.data_dir), self.health.summary(),
                                untrusted=untrusted, tools=bool(tools))
        if pick is None:
            return model
        target = find_model(installed, pick.model)
        if target is None:
            return model
        log_event(_log, "model_auto_switched", old=model.name, new=target.name)
        self.last_route = {"role": "auto-switch", "model": target.name, "switched": pick.reason,
                           "fallback": self._announce_once(f"AI_AUTO_SWITCH moved from {model.name} ({pick.reason})")}
        return target

    def _record_failure(self, model: str, exc: Exception) -> None:
        """Count a failure against the model's health - except 'the conversation is too long', which isn't its fault."""
        if isinstance(exc, AIRequestError) and "context" in str(exc).lower():
            return
        self.health.failure(model, f"{exc.__class__.__name__}: {exc}")

    last_route: dict | None = None  # the model the last request used (and a fallback, if one happened)

    def _announce_once(self, reason: str) -> str | None:
        """A model substitution is told to you the first time it happens (then only logged): honest, not noisy."""
        seen = self.__dict__.setdefault("_announced", set())
        if reason in seen:
            return None
        seen.add(reason)
        return reason

    async def _fallback(self, request: ChatRequest, exc: Exception) -> ChatRequest | None:
        """A role model (fast/smart/coding) failed: try AI_MODEL once. None when there's nothing to fall back to."""
        if isinstance(exc, AIRequestError) and "context" in str(exc).lower():
            return None  # too long for the model: the conversation shortens it instead
        if isinstance(exc, ModelNotFoundError):
            self._models = None
        default, _ = await self.resolve_model()
        if default.name == request.model:
            return None
        log_event(_log, "model_fallback", failed=request.model, used=default.name, reason=exc.__class__.__name__)
        self.last_route = {"role": "default", "model": default.name,
                           "fallback": f"{request.model} failed ({exc.__class__.__name__})"}
        think = self.settings.ai_think if default.supports_thinking else None
        return ChatRequest(model=default.name, messages=request.messages, tools=request.tools, think=think)

    async def chat(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = (),
                   fast: bool = False, smart: bool = False, coding: bool = False,
                   sensitive: bool = False, untrusted: bool = False) -> ChatResponse:
        request = await self._prepare(messages, tools, fast, smart, coding, sensitive, untrusted)
        started = time.monotonic()
        try:
            response = await self.provider.chat(request)
            self.health.success(request.model, time.monotonic() - started)
            return response
        except (ModelNotFoundError, AITimeoutError, AIRequestError) as exc:
            self._record_failure(request.model, exc)
            retry = await self._fallback(request, exc)
            if retry is None:
                if isinstance(exc, ModelNotFoundError):
                    self._models = None  # model was removed; re-detect next time
                raise
            started = time.monotonic()
            try:
                response = await self.provider.chat(retry)
            except (ModelNotFoundError, AITimeoutError, AIRequestError) as second:
                self._record_failure(retry.model, second)
                raise
            self.health.success(retry.model, time.monotonic() - started)
            return response

    async def stream_chat(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = (), fast: bool = False,
        smart: bool = False, coding: bool = False, sensitive: bool = False,
        untrusted: bool = False,
    ) -> AsyncIterator[tuple[str, ChatChunk]]:
        """Yields (model name, chunk). ``fast``: AI_FAST_MODEL, ``smart``: AI_SMART_MODEL, ``coding``:
        AI_CODING_MODEL (if set and installed). A failure before the first chunk falls back to AI_MODEL once."""
        request = await self._prepare(messages, tools, fast, smart, coding, sensitive, untrusted)
        started = False
        began = time.monotonic()
        try:
            async for chunk in self.provider.stream_chat(request):
                if not started:
                    self.health.first_token(request.model, time.monotonic() - began)
                started = True
                yield request.model, chunk
            self.health.success(request.model, time.monotonic() - began)
        except (ModelNotFoundError, AITimeoutError, AIRequestError) as exc:
            self._record_failure(request.model, exc)
            retry = None if started else await self._fallback(request, exc)
            if retry is None:
                if isinstance(exc, ModelNotFoundError):
                    self._models = None
                raise
            async for chunk in self.provider.stream_chat(retry):
                yield retry.model, chunk

    # ------------------------------------------------------------------- vision

    async def vision_model(self) -> ModelInfo | None:
        """VISION_MODEL, else the current chat model if it can see images, else the smallest vision model.

        None when no installed model can read images.
        """
        models = await self.models()
        wanted = self.settings.vision_model
        if wanted:
            model = find_model(models, wanted)
            if model is None:
                installed = ", ".join(m.name for m in models if m.can_chat) or "none"
                raise ModelNotFoundError(
                    f"The vision model '{wanted}' (VISION_MODEL in .env) is not installed in Ollama. "
                    f"Installed: {installed}.")
            return model
        vision = [m for m in models if can_see_images(m)]
        if not vision:
            return None
        try:
            current, _ = await self.resolve_model()
        except AIError:
            current = None
        if current is not None and any(m.name == current.name for m in vision):
            return current  # already loaded, no model swap
        return min(vision, key=lambda m: m.size_bytes or 0)

    async def vision_chat(self, model: ModelInfo, system: str, prompt: str, image: bytes) -> ChatResponse:
        """One question about one image. The image is sent to the local provider only."""
        request = ChatRequest(model=model.name, messages=(ChatMessage.system(system),
                                                          ChatMessage.user(prompt, (image,))))
        try:
            return await self.provider.chat(request)
        except ModelNotFoundError:
            self._models = None
            raise

    async def aclose(self) -> None:
        await self.provider.aclose()
