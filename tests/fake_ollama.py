"""An in-process fake of the Ollama HTTP API for tests (httpx.MockTransport)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


def model_entry(name: str, modified: str = "2026-01-01T00:00:00Z", family: str = "llama") -> dict[str, Any]:
    return {
        "name": name,
        "model": name,
        "modified_at": modified,
        "size": 2_000_000_000,
        "details": {"family": family, "parameter_size": "3B", "quantization_level": "Q4_K_M"},
    }


@dataclass
class FakeOllama:
    models: list[dict[str, Any]] = field(default_factory=list)
    capabilities: dict[str, list[str] | None] = field(default_factory=dict)
    reply: dict[str, Any] = field(default_factory=lambda: {"role": "assistant", "content": "Hello."})
    replies: list[dict[str, Any]] = field(default_factory=list)  # consumed in order, then ``reply``
    stream_parts: list[str] | None = None
    chat_status: int = 200
    chat_error: str | None = None
    raise_on: dict[str, Exception] = field(default_factory=dict)
    requests: list[tuple[str, str, Any]] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def last_chat_payload(self) -> dict[str, Any]:
        return next(body for method, path, body in reversed(self.requests) if path == "/api/chat")

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        if path in self.raise_on:
            raise self.raise_on[path]
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.0"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": self.models})
        if path == "/api/show":
            caps = self.capabilities.get(body["model"], ["completion", "tools"])
            data: dict[str, Any] = {"details": {}}
            if caps is not None:
                data["capabilities"] = caps
            return httpx.Response(200, json=data)
        if path == "/api/generate":  # warm-up (load the model)
            return httpx.Response(200, json={"model": body.get("model"), "response": "", "done": True})
        if path == "/api/chat":
            if self.chat_error:
                return httpx.Response(self.chat_status, json={"error": self.chat_error})
            reply = self.replies.pop(0) if self.replies else self.reply
            if body.get("stream"):
                parts = self.stream_parts if self.stream_parts is not None and not self.replies else [reply.get("content", "")]
                lines = [
                    json.dumps({"model": body["model"], "message": {"role": "assistant", "content": p}, "done": False})
                    for p in parts
                ]
                lines.append(
                    json.dumps(
                        {
                            "model": body["model"],
                            "message": {"role": "assistant", "content": "", **{k: v for k, v in reply.items() if k == "tool_calls"}},
                            "done": True, "eval_count": 5, "prompt_eval_count": 40, "total_duration": 1_500_000_000,
                        }
                    )
                )
                return httpx.Response(200, content="\n".join(lines).encode())
            return httpx.Response(
                200,
                json={"model": body["model"], "message": reply, "done": True, "eval_count": 5,
                      "prompt_eval_count": 40, "total_duration": 1_500_000_000},
            )
        return httpx.Response(404, json={"error": "not found"})
