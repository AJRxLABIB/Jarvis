"""Provider-neutral AI types and the :class:`AIProvider` interface.

Every backend (Ollama now; optional cloud providers later) converts between
these types and its own wire format, so the rest of the app never depends on a
specific provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]

# ------------------------------------------------------------------- exceptions


class AIError(Exception):
    """Base class. ``str(exc)`` is always safe to show to the user."""


class ProviderUnavailableError(AIError):
    """The AI backend cannot be reached (not running, offline mode, ...)."""


class NoModelAvailableError(AIError):
    """The backend is running but has no usable chat model."""


class ModelNotFoundError(AIError):
    """The requested model is not installed."""


class ModelCapabilityError(AIError):
    """The model cannot do what was asked (e.g. tool calling, chat)."""


class AITimeoutError(AIError):
    """The backend did not answer in time."""


class AIRequestError(AIError):
    """The backend returned an error or an invalid response."""


# ------------------------------------------------------------------------ types


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may request. ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass(frozen=True)
class ToolCall:
    """A tool request produced by the model. Always untrusted input."""

    name: str
    arguments: Mapping[str, Any]
    id: str | None = None
    parse_error: str | None = None  # set when the model sent malformed arguments


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_name: str | None = None  # for role == "tool": which tool produced this result
    images: tuple[bytes, ...] = ()  # PNG/JPEG images for vision models (kept in memory only)

    @classmethod
    def system(cls, content: str) -> "ChatMessage":
        return cls("system", content)

    @classmethod
    def user(cls, content: str, images: Sequence[bytes] = ()) -> "ChatMessage":
        return cls("user", content, images=tuple(images))

    @classmethod
    def assistant(cls, content: str, tool_calls: Sequence[ToolCall] = ()) -> "ChatMessage":
        return cls("assistant", content, tuple(tool_calls))

    @classmethod
    def tool(cls, tool_name: str, content: str) -> "ChatMessage":
        return cls("tool", content, tool_name=tool_name)


@dataclass(frozen=True)
class ModelInfo:
    name: str
    size_bytes: int | None = None
    family: str | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    modified_at: str | None = None
    capabilities: frozenset[str] | None = None  # None = backend did not report them

    @property
    def is_embedding_only(self) -> bool:
        if self.capabilities is not None:
            return "embedding" in self.capabilities and "completion" not in self.capabilities
        hint = f"{self.name} {self.family or ''}".lower()
        return "embed" in hint or "bert" in hint

    @property
    def can_chat(self) -> bool:
        return not self.is_embedding_only

    @property
    def supports_tools(self) -> bool | None:
        return None if self.capabilities is None else "tools" in self.capabilities

    @property
    def supports_vision(self) -> bool:
        return bool(self.capabilities and "vision" in self.capabilities)

    @property
    def supports_thinking(self) -> bool:
        return bool(self.capabilities and "thinking" in self.capabilities)


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: Sequence[ChatMessage]
    tools: Sequence[ToolSpec] = ()
    think: bool | None = None  # only sent to models that support thinking


@dataclass(frozen=True)
class ChatStats:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_ms: int | None = None
    load_ms: int | None = None
    prompt_ms: int | None = None  # reading the prompt (slow when the model doesn't fit on the GPU)
    eval_ms: int | None = None  # writing the answer


@dataclass(frozen=True)
class ChatResponse:
    model: str
    message: ChatMessage
    stats: ChatStats = ChatStats()


@dataclass(frozen=True)
class ChatChunk:
    """One streamed piece of a response. ``done`` marks the final chunk."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    done: bool = False
    stats: ChatStats | None = None


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    version: str | None = None


# -------------------------------------------------------------------- interface


class AIProvider(ABC):
    """Interface every AI backend implements."""

    name: str = "base"

    @abstractmethod
    async def health(self) -> ProviderHealth:
        """Return backend information, or raise ProviderUnavailableError."""

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        """Installed/available models (capabilities may be None)."""

    async def model_capabilities(self, model: str) -> frozenset[str] | None:
        """Capabilities of one model, if the backend can report them."""
        return None

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Complete one chat turn."""

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Stream a chat turn. Default: one chunk from :meth:`chat`."""
        response = await self.chat(request)
        yield ChatChunk(
            content=response.message.content,
            tool_calls=response.message.tool_calls,
            done=True,
            stats=response.stats,
        )

    async def warm_up(self, model: str) -> None:
        """Load ``model`` into memory ahead of the first request (optional)."""

    async def aclose(self) -> None:
        """Release network resources."""
