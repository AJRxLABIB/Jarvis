"""Ollama provider (default, fully local).

API used: GET /api/version, GET /api/tags, POST /api/show, POST /api/chat.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from app import __version__
from app.ai.base import (
    AIProvider,
    AIRequestError,
    AITimeoutError,
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatStats,
    ModelCapabilityError,
    ModelInfo,
    ModelNotFoundError,
    ProviderHealth,
    ProviderUnavailableError,
    ToolCall,
    ToolSpec,
)

OLLAMA_NOT_RUNNING = "Ollama is not running. Start Ollama and try again."
_HEALTH_TIMEOUT = 3.0
_THINK_BLOCK = re.compile(r"<think>.*?(</think>|$)", re.S)


def strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks some models put in content. "Thinking-only" models
    (e.g. qwen3 2507 thinking) get the opening tag from their template, so only "</think>" appears:
    everything before it is reasoning too."""
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def _partial_suffix(text: str, token: str) -> int:
    """Length of the longest end of ``text`` that is a prefix of ``token``."""
    for size in range(min(len(token) - 1, len(text)), 0, -1):
        if text.endswith(token[:size]):
            return size
    return 0


class ThinkFilter:
    """Streaming version of :func:`strip_think` (tags may be split across chunks)."""

    OPEN, CLOSE = "<think>", "</think>"

    HOLD_LIMIT = 1500  # hidden_open: characters held back while waiting for a bare "</think>"

    def __init__(self, hidden_open: bool = False) -> None:
        self._buffer = ""
        self._inside = False
        # hidden_open: the model may think without writing "<think>" (only "</think>" at the end of the
        # reasoning). Hold the start back until "</think>" shows up, or it's clearly a normal answer.
        self._holding = hidden_open
        self._held = ""

    def feed(self, text: str) -> str:
        if self._holding:
            self._held += text
            index = self._held.find(self.CLOSE)
            if index >= 0:
                text, self._held, self._holding = self._held[index + len(self.CLOSE):].lstrip(), "", False
            elif self.OPEN in self._held or len(self._held) > self.HOLD_LIMIT:
                text, self._held, self._holding = self._held, "", False  # tagged normally, or a long answer
            else:
                return ""
        self._buffer += text
        out: list[str] = []
        while True:
            if self._inside:
                index = self._buffer.find(self.CLOSE)
                if index < 0:
                    self._buffer = self._buffer[-(len(self.CLOSE) - 1):]
                    break
                self._buffer = self._buffer[index + len(self.CLOSE):]
                self._inside = False
            else:
                index = self._buffer.find(self.OPEN)
                if index < 0:
                    keep = _partial_suffix(self._buffer, self.OPEN)
                    out.append(self._buffer[: len(self._buffer) - keep])
                    self._buffer = self._buffer[len(self._buffer) - keep:]
                    break
                out.append(self._buffer[:index])
                self._buffer = self._buffer[index + len(self.OPEN):]
                self._inside = True
        return "".join(out)

    def flush(self) -> str:
        if self._holding:  # no "</think>" came: it was an ordinary (short) answer
            self._holding = False
            held = self._held
            self._held = ""
            return self.feed(held) + self.flush()
        rest = "" if self._inside else self._buffer
        self._buffer = ""
        return rest


def is_loopback_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _keep_alive_value(value: str) -> str | int:
    return int(value) if re.fullmatch(r"-?\d+", value) else value


def _tool_to_wire(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": dict(tool.parameters)},
    }


def _message_to_wire(message: ChatMessage) -> dict[str, Any]:
    data: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        data["tool_calls"] = [
            {"function": {"name": call.name, "arguments": dict(call.arguments)}} for call in message.tool_calls
        ]
    if message.role == "tool" and message.tool_name:
        data["tool_name"] = message.tool_name
    if message.images:
        data["images"] = [base64.b64encode(image).decode("ascii") for image in message.images]
    return data


def _parse_tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    if not isinstance(raw, list):
        return ()
    calls: list[ToolCall] = []
    for item in raw:
        function = item.get("function") if isinstance(item, Mapping) else None
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            continue
        arguments: Any = function.get("arguments", {})
        error: str | None = None
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                arguments, error = {}, f"arguments are not valid JSON: {exc.msg}"
        if not isinstance(arguments, Mapping):
            arguments, error = {}, "arguments must be a JSON object"
        call_id = item.get("id") if isinstance(item.get("id"), str) else None
        calls.append(ToolCall(name=function["name"], arguments=dict(arguments), id=call_id, parse_error=error))
    return tuple(calls)


def _ns_to_ms(value: Any) -> int | None:
    return int(value) // 1_000_000 if isinstance(value, (int, float)) else None


def _parse_stats(data: Mapping[str, Any]) -> ChatStats:
    return ChatStats(
        prompt_tokens=data.get("prompt_eval_count"),
        completion_tokens=data.get("eval_count"),
        total_ms=_ns_to_ms(data.get("total_duration")),
        load_ms=_ns_to_ms(data.get("load_duration")),
        prompt_ms=_ns_to_ms(data.get("prompt_eval_duration")),
        eval_ms=_ns_to_ms(data.get("eval_duration")),
    )


class OllamaProvider(AIProvider):
    name = "ollama"

    def __init__(
        self,
        host: str,
        *,
        request_timeout: float = 120.0,
        connect_timeout: float = 3.0,
        keep_alive: str = "10m",
        context_window: int = 8192,
        temperature: float = 0.3,
        offline_mode: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.request_timeout = request_timeout
        self.connect_timeout = connect_timeout
        self.keep_alive = _keep_alive_value(keep_alive)
        self.context_window = context_window
        self.temperature = temperature
        self.offline_mode = offline_mode
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ plumbing

    def _client_for_request(self) -> httpx.AsyncClient:
        if self.offline_mode and not is_loopback_host(self.host):
            raise ProviderUnavailableError(
                f"Offline mode is on, and the Ollama host {self.host} is not on this computer."
            )
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.host,
                timeout=httpx.Timeout(self.request_timeout, connect=self.connect_timeout),
                transport=self._transport,
                headers={"User-Agent": f"JarvisAssistant/{__version__}"},
                trust_env=False,  # never route local Ollama traffic through a system proxy
            )
        return self._client

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, Mapping) and data.get("error"):
                return str(data["error"])
        except ValueError:
            pass
        return response.text[:300] or response.reason_phrase

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message = self._error_text(response)
        lowered = message.lower()
        if response.status_code == 404 and "not found" in lowered:
            raise ModelNotFoundError(f"Ollama could not find the model: {message}")
        if "does not support" in lowered:
            raise ModelCapabilityError(f"The model cannot do this: {message}")
        raise AIRequestError(f"Ollama returned HTTP {response.status_code}: {message}")

    async def _request(
        self, method: str, path: str, *, payload: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        client = self._client_for_request()
        try:
            response = await client.request(
                method, path, json=payload, timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailableError(OLLAMA_NOT_RUNNING) from exc
        except httpx.TimeoutException as exc:
            raise AITimeoutError("Ollama took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise AIRequestError(f"Could not talk to Ollama: {exc}") from exc
        self._raise_for_status(response)
        if not response.content:  # e.g. /api/delete answers 200 with no body
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise AIRequestError("Ollama returned a response that is not valid JSON.") from exc

    def _chat_payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [_message_to_wire(m) for m in request.messages],
            "stream": stream,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_ctx": self.context_window},
        }
        if request.tools:
            payload["tools"] = [_tool_to_wire(t) for t in request.tools]
        if request.think is not None:
            payload["think"] = request.think
        if request.format is not None:                # constrained decoding: the reply must be JSON matching this schema
            payload["format"] = request.format
            payload["options"] = {**payload["options"], "temperature": 0}
        return payload

    # ------------------------------------------------------------------ interface

    async def health(self) -> ProviderHealth:
        data = await self._request("GET", "/api/version", timeout=_HEALTH_TIMEOUT)
        version = data.get("version") if isinstance(data, Mapping) else None
        return ProviderHealth(provider=self.name, version=str(version) if version else None)

    async def list_models(self) -> list[ModelInfo]:
        data = await self._request("GET", "/api/tags", timeout=10.0)
        models: list[ModelInfo] = []
        for item in (data.get("models") or []) if isinstance(data, Mapping) else []:
            if not isinstance(item, Mapping):
                continue
            name = item.get("name") or item.get("model")
            if not isinstance(name, str) or not name:
                continue
            details = item.get("details") if isinstance(item.get("details"), Mapping) else {}
            models.append(
                ModelInfo(
                    name=name,
                    size_bytes=item.get("size") if isinstance(item.get("size"), int) else None,
                    family=details.get("family"),
                    parameter_size=details.get("parameter_size"),
                    quantization=details.get("quantization_level"),
                    modified_at=item.get("modified_at"),
                )
            )
        return models

    async def model_capabilities(self, model: str) -> frozenset[str] | None:
        data = await self._request("POST", "/api/show", payload={"model": model}, timeout=10.0)
        capabilities = data.get("capabilities") if isinstance(data, Mapping) else None
        if not isinstance(capabilities, list):
            return None  # older Ollama versions do not report capabilities
        return frozenset(str(c).lower() for c in capabilities)

    async def model_details(self, model: str) -> dict[str, Any]:
        """What the model's own metadata says (``/api/show``): the start of its licence text and its trained context length. Missing fields stay missing."""
        data = await self._request("POST", "/api/show", payload={"model": model}, timeout=10.0)
        if not isinstance(data, Mapping):
            return {}
        info = data.get("model_info") if isinstance(data.get("model_info"), Mapping) else {}
        context = next((v for k, v in info.items() if str(k).endswith(".context_length") and isinstance(v, int)), None)
        licence = " ".join(str(data.get("license") or "").split())[:160]
        return {"license": licence or None, "context_length": context}

    async def chat(self, request: ChatRequest) -> ChatResponse:
        data = await self._request("POST", "/api/chat", payload=self._chat_payload(request, stream=False))
        if not isinstance(data, Mapping) or not isinstance(data.get("message"), Mapping):
            raise AIRequestError("Ollama returned an unexpected chat response.")
        message = data["message"]
        return ChatResponse(
            model=str(data.get("model") or request.model),
            message=ChatMessage.assistant(
                strip_think(str(message.get("content") or "")), _parse_tool_calls(message.get("tool_calls"))
            ),
            stats=_parse_stats(data),
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        client = self._client_for_request()
        think = ThinkFilter(hidden_open=request.think is False)
        try:
            async with client.stream("POST", "/api/chat", json=self._chat_payload(request, stream=True)) as response:
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise AIRequestError("Ollama sent a malformed stream line.") from exc
                    if data.get("error"):
                        raise AIRequestError(f"Ollama error: {data['error']}")
                    message = data.get("message") if isinstance(data.get("message"), Mapping) else {}
                    done = bool(data.get("done"))
                    text = think.feed(str(message.get("content") or ""))
                    if done:
                        text += think.flush()
                    yield ChatChunk(
                        content=text,
                        tool_calls=_parse_tool_calls(message.get("tool_calls")),
                        done=done,
                        stats=_parse_stats(data) if done else None,
                    )
                    if done:
                        return
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailableError(OLLAMA_NOT_RUNNING) from exc
        except httpx.TimeoutException as exc:
            raise AITimeoutError("Ollama took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise AIRequestError(f"Could not talk to Ollama: {exc}") from exc
        raise AIRequestError("Ollama closed the stream before finishing.")

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """Embeddings for ``texts`` (Ollama /api/embed)."""
        data = await self._request("POST", "/api/embed", payload={"model": model, "input": list(texts),
                                                                  "keep_alive": self.keep_alive})
        vectors = data.get("embeddings") if isinstance(data, Mapping) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise AIRequestError("Ollama returned no embeddings.")
        return vectors

    async def pull(self, model: str, on_progress=None) -> None:
        """Download a model (Ollama /api/pull). on_progress(status, completed, total) while it runs."""
        client = self._client_for_request()
        try:
            async with client.stream("POST", "/api/pull", json={"model": model, "stream": True},
                                     timeout=httpx.Timeout(None, connect=self.connect_timeout)) as response:
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise AIRequestError(f"Ollama could not download {model}: {data['error']}")
                    if on_progress is not None:
                        on_progress(str(data.get("status", "")), data.get("completed"), data.get("total"))
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailableError(OLLAMA_NOT_RUNNING) from exc
        except httpx.HTTPError as exc:
            raise AIRequestError(f"Could not download {model}: {exc}") from exc

    async def create(self, name: str, base: str, system: str = "", parameters: Mapping[str, Any] | None = None) -> None:
        """Make a model called ``name`` from an installed ``base`` model (Ollama /api/create). The weights are
        shared with the base, so this takes seconds and almost no disk space."""
        payload: dict[str, Any] = {"model": name, "from": base, "stream": False}
        if system:
            payload["system"] = system
        if parameters:
            payload["parameters"] = dict(parameters)
        try:
            await self._request("POST", "/api/create", payload=payload, timeout=600.0)
        except AIRequestError as exc:  # older Ollama: a Modelfile instead of "from"
            if "from" not in str(exc).lower() and "modelfile" not in str(exc).lower():
                raise
            lines = [f"FROM {base}"] + [f"PARAMETER {k} {v}" for k, v in (parameters or {}).items()]
            if system:
                lines.append("SYSTEM " + json.dumps(system))
            await self._request("POST", "/api/create", payload={"model": name, "modelfile": "\n".join(lines),
                                                               "stream": False}, timeout=600.0)

    async def delete(self, name: str) -> None:
        """Remove an installed model (Ollama /api/delete). Weights still used by another model are kept."""
        await self._request("DELETE", "/api/delete", payload={"model": name}, timeout=60.0)

    async def unload(self, model: str) -> None:
        """Free the model's (GPU) memory now; the next request loads it again."""
        await self._request("POST", "/api/generate", payload={"model": model, "prompt": "", "keep_alive": 0})

    async def warm_up(self, model: str) -> None:
        """An empty generate request makes Ollama load the model (and keep it loaded for keep_alive),
        so the first real question doesn't wait several seconds for the model to load."""
        # With the same num_ctx the chat requests use: Ollama reloads a model whenever num_ctx changes, and a
        # reload while speech recognition holds GPU memory can put the model partly on the CPU (the first
        # question then took over two minutes).
        await self._request("POST", "/api/generate", payload={
            "model": model, "prompt": "", "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.context_window}})

    async def gpu_share(self, model: str) -> float | None:
        """How much of the loaded model is in GPU memory (1.0 = all of it; less = partly on the CPU, slow).
        None if the model isn't loaded or Ollama doesn't say."""
        data = await self._request("GET", "/api/ps")
        for entry in (data or {}).get("models", []) or []:
            name = str(entry.get("name") or entry.get("model") or "")
            if name.removesuffix(":latest") == model.removesuffix(":latest"):
                size, vram = entry.get("size"), entry.get("size_vram")
                if isinstance(size, (int, float)) and size > 0 and isinstance(vram, (int, float)):
                    return max(0.0, min(1.0, vram / size))
        return None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
