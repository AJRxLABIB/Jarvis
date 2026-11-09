"""Microsoft Foundry Local as an OPTIONAL inference provider (next to Ollama, never instead of it).

Foundry Local serves an OpenAI-compatible REST API on this computer (``GET /v1/models``, ``POST /v1/chat/completions``). Its port is chosen when its service
starts, so the address is configured (``FOUNDRY_LOCAL_URL``, find it with ``foundry service status``); nothing is guessed, discovered on the network or downloaded.
The address must be on this computer (loopback): a local engine is the point of this provider.

What is NOT assumed: the endpoint does not report model capabilities (vision / tools), so ``capabilities`` stays unknown (None) rather than invented; tool calling
is sent only when the request has tools and a model that rejects them raises a clear error. Embeddings are not offered here.

Honest status: wire format covered by tests with a fake transport; not run against a real Foundry Local service (not installed in the test environment).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app import __version__
from app.ai.base import (AIError, AIProvider, AIRequestError, AITimeoutError, ChatChunk, ChatMessage, ChatRequest, ChatResponse, ChatStats, ModelInfo,
                         ModelNotFoundError, ProviderHealth, ProviderUnavailableError, ToolCall)
from app.ai.ollama import is_loopback_host


def _wire_messages(messages) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    for n, m in enumerate(messages):
        if m.role == "assistant" and m.tool_calls:
            calls = []
            pending_ids = []
            for i, c in enumerate(m.tool_calls):
                cid = c.id or f"call_{n}_{i}"
                pending_ids.append(cid)
                calls.append({"id": cid, "type": "function", "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))}})
            out.append({"role": "assistant", "content": m.content or None, "tool_calls": calls})
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": pending_ids.pop(0) if pending_ids else f"call_{n}", "content": m.content})
        elif m.images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
            for img in m.images:
                parts.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(img).decode("ascii")}})
            out.append({"role": m.role, "content": parts})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def _parse_tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    calls = []
    for item in raw or ():
        fn = (item or {}).get("function") or {}
        text, args, err = fn.get("arguments"), {}, None
        if isinstance(text, dict):
            args = text
        elif isinstance(text, str) and text.strip():
            try:
                value = json.loads(text)
                args = value if isinstance(value, dict) else {}
                err = None if isinstance(value, dict) else "arguments were not a JSON object"
            except ValueError:
                err = "arguments were not valid JSON"
        calls.append(ToolCall(str(fn.get("name") or ""), args, item.get("id"), err))
    return tuple(calls)


def _stats(data: dict[str, Any]) -> ChatStats:
    usage = data.get("usage") or {}
    return ChatStats(prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))


class FoundryLocalProvider(AIProvider):
    name = "foundry_local"

    def __init__(self, base_url: str, *, request_timeout: float = 120.0, connect_timeout: float = 3.0, context_window: int = 8192, temperature: float = 0.3,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not base_url:
            raise ProviderUnavailableError("Foundry Local is not configured. Start its service, run 'foundry service status', and set FOUNDRY_LOCAL_URL to the address it shows.")
        if not is_loopback_host(base_url):
            raise ProviderUnavailableError("Foundry Local must run on this computer (a loopback address such as http://127.0.0.1:PORT).")
        self.base_url = base_url.rstrip("/")
        self.request_timeout, self.connect_timeout = request_timeout, connect_timeout
        self.context_window, self.temperature = context_window, temperature
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(self.request_timeout, connect=self.connect_timeout), transport=self._transport,
                                             headers={"User-Agent": f"JarvisAssistant/{__version__}"}, trust_env=False)
        return self._client

    async def _json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            response = await self._http().request(method, path, **kw)
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError("Foundry Local is not answering. Is its service running ('foundry service status')?") from exc
        except httpx.TimeoutException as exc:
            raise AITimeoutError("Foundry Local took too long to answer.") from exc
        except httpx.HTTPError as exc:
            raise AIRequestError(f"Foundry Local request failed: {exc}") from exc
        if response.status_code == 404:
            raise ModelNotFoundError("Foundry Local does not have that model loaded or downloaded.")
        if response.status_code >= 400:
            raise AIRequestError(f"Foundry Local returned HTTP {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise AIRequestError("Foundry Local sent an answer that is not JSON.") from exc
        return data if isinstance(data, dict) else {}

    def _payload(self, request: ChatRequest, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"model": request.model, "messages": _wire_messages(request.messages), "stream": stream, "temperature": self.temperature}
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": dict(t.parameters)}} for t in request.tools]
        return body

    async def health(self) -> ProviderHealth:
        await self._json("GET", "/v1/models")
        return ProviderHealth(self.name, None)                      # the endpoint reports no version: None, not a guess

    async def list_models(self) -> list[ModelInfo]:
        data = await self._json("GET", "/v1/models")
        return [ModelInfo(name=str(m.get("id")), family=m.get("owned_by")) for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]

    async def chat(self, request: ChatRequest) -> ChatResponse:
        data = await self._json("POST", "/v1/chat/completions", json=self._payload(request, False))
        choices = data.get("choices") or []
        if not choices:
            raise AIRequestError("Foundry Local answered without a choice.")
        msg = choices[0].get("message") or {}
        return ChatResponse(request.model, ChatMessage.assistant(msg.get("content") or "", _parse_tool_calls(msg.get("tool_calls"))), _stats(data))

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        partial: dict[int, dict[str, Any]] = {}
        try:
            async with self._http().stream("POST", "/v1/chat/completions", json=self._payload(request, True)) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise AIRequestError(f"Foundry Local returned HTTP {response.status_code}: {response.text[:200]}")
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        for tc in delta.get("tool_calls") or []:
                            slot = partial.setdefault(int(tc.get("index", 0)), {"id": tc.get("id"), "function": {"name": "", "arguments": ""}})
                            fn = tc.get("function") or {}
                            slot["function"]["name"] += fn.get("name") or ""
                            slot["function"]["arguments"] += fn.get("arguments") or ""
                            slot["id"] = slot["id"] or tc.get("id")
                        if delta.get("content"):
                            yield ChatChunk(content=delta["content"])
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError("Foundry Local is not answering. Is its service running ('foundry service status')?") from exc
        except httpx.TimeoutException as exc:
            raise AITimeoutError("Foundry Local took too long to answer.") from exc
        except httpx.HTTPError as exc:
            raise AIRequestError(f"Foundry Local request failed: {exc}") from exc
        yield ChatChunk(tool_calls=_parse_tool_calls([partial[i] for i in sorted(partial)]), done=True)

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


__all__ = ["FoundryLocalProvider", "AIError"]
