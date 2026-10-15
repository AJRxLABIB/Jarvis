from __future__ import annotations

import asyncio

import httpx
import pytest

from app.ai.base import (
    AIRequestError,
    AITimeoutError,
    ChatMessage,
    ChatRequest,
    ModelCapabilityError,
    ModelNotFoundError,
    ProviderUnavailableError,
    ToolSpec,
)
from app.ai.ollama import OLLAMA_NOT_RUNNING, OllamaProvider, ThinkFilter, is_loopback_host, strip_think
from tests.fake_ollama import FakeOllama, model_entry

HOST = "http://127.0.0.1:11434"


def provider(fake: FakeOllama, **kwargs) -> OllamaProvider:
    return OllamaProvider(HOST, transport=fake.transport(), **kwargs)


def run(coro):
    return asyncio.run(coro)


async def _call(p: OllamaProvider, method: str, *args):
    try:
        return await getattr(p, method)(*args)
    finally:
        await p.aclose()


def test_health_and_models() -> None:
    fake = FakeOllama(models=[model_entry("llama3.2:latest")])
    assert run(_call(provider(fake), "health")).version == "0.12.0"
    (model,) = run(_call(provider(fake), "list_models"))
    assert model.name == "llama3.2:latest" and model.parameter_size == "3B"


def test_connection_refused_gives_friendly_error() -> None:
    fake = FakeOllama(raise_on={"/api/version": httpx.ConnectError("refused")})
    with pytest.raises(ProviderUnavailableError) as info:
        run(_call(provider(fake), "health"))
    assert str(info.value) == OLLAMA_NOT_RUNNING


def test_timeout_mapped() -> None:
    fake = FakeOllama(raise_on={"/api/chat": httpx.ReadTimeout("slow")})
    with pytest.raises(AITimeoutError):
        run(_call(provider(fake), "chat", ChatRequest("m", [ChatMessage.user("hi")])))


def test_capabilities_reported_or_none() -> None:
    fake = FakeOllama(capabilities={"a": ["completion", "tools"], "b": None})
    assert run(_call(provider(fake), "model_capabilities", "a")) == frozenset({"completion", "tools"})
    assert run(_call(provider(fake), "model_capabilities", "b")) is None


def test_chat_payload_and_response() -> None:
    fake = FakeOllama(reply={"role": "assistant", "content": "<think>hmm</think>\n\nOpening Chrome."})
    tool = ToolSpec("open_application", "Open an app", {"type": "object", "properties": {"application": {"type": "string"}}})
    request = ChatRequest(
        "llama3.2", [ChatMessage.system("sys"), ChatMessage.user("open chrome"), ChatMessage.tool("open_application", "ok")],
        tools=[tool], think=False,
    )
    response = run(_call(provider(fake, keep_alive="-1", context_window=4096, temperature=0.1), "chat", request))
    assert response.message.content == "Opening Chrome."
    assert response.stats.total_ms == 1500 and response.stats.completion_tokens == 5
    payload = fake.last_chat_payload()
    assert payload["stream"] is False and payload["keep_alive"] == -1 and payload["think"] is False
    assert payload["options"] == {"temperature": 0.1, "num_ctx": 4096}
    assert payload["tools"][0]["function"]["name"] == "open_application"
    assert payload["messages"][2] == {"role": "tool", "content": "ok", "tool_name": "open_application"}


def test_think_omitted_when_none() -> None:
    fake = FakeOllama()
    run(_call(provider(fake), "chat", ChatRequest("m", [ChatMessage.user("hi")])))
    assert "think" not in fake.last_chat_payload() and "tools" not in fake.last_chat_payload()


def test_tool_calls_parsed_including_string_and_bad_arguments() -> None:
    fake = FakeOllama(
        reply={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "open_application", "arguments": {"application": "chrome"}}},
                {"function": {"name": "set_volume", "arguments": '{"level": 30}'}},
                {"function": {"name": "broken", "arguments": "{not json"}},
                {"nonsense": True},
            ],
        }
    )
    calls = run(_call(provider(fake), "chat", ChatRequest("m", [ChatMessage.user("x")]))).message.tool_calls
    assert [c.name for c in calls] == ["open_application", "set_volume", "broken"]
    assert calls[0].arguments == {"application": "chrome"}
    assert calls[1].arguments == {"level": 30}
    assert calls[2].arguments == {} and calls[2].parse_error


def test_error_mapping() -> None:
    fake = FakeOllama(chat_status=404, chat_error='model "ghost" not found, try pulling it first')
    with pytest.raises(ModelNotFoundError):
        run(_call(provider(fake), "chat", ChatRequest("ghost", [ChatMessage.user("x")])))
    fake = FakeOllama(chat_status=400, chat_error="registry.ollama.ai/library/gemma does not support tools")
    with pytest.raises(ModelCapabilityError):
        run(_call(provider(fake), "chat", ChatRequest("gemma", [ChatMessage.user("x")])))
    fake = FakeOllama(chat_status=500, chat_error="out of memory")
    with pytest.raises(AIRequestError, match="out of memory"):
        run(_call(provider(fake), "chat", ChatRequest("m", [ChatMessage.user("x")])))


def test_streaming_strips_think_across_chunks() -> None:
    fake = FakeOllama(stream_parts=["<thi", "nk>secret plan</th", "ink>Hel", "lo <", "b>there"])

    async def collect():
        p = provider(fake)
        try:
            return [c async for c in p.stream_chat(ChatRequest("m", [ChatMessage.user("hi")]))]
        finally:
            await p.aclose()

    chunks = run(collect())
    assert "".join(c.content for c in chunks) == "Hello <b>there"
    assert chunks[-1].done and chunks[-1].stats.prompt_tokens == 40


def test_stream_errors_mapped() -> None:
    fake = FakeOllama(raise_on={"/api/chat": httpx.ConnectError("refused")})

    async def collect():
        p = provider(fake)
        try:
            return [c async for c in p.stream_chat(ChatRequest("m", [ChatMessage.user("hi")]))]
        finally:
            await p.aclose()

    with pytest.raises(ProviderUnavailableError):
        run(collect())


def test_offline_mode_blocks_remote_host_only() -> None:
    fake = FakeOllama()
    remote = OllamaProvider("http://192.168.1.50:11434", offline_mode=True, transport=fake.transport())
    with pytest.raises(ProviderUnavailableError, match="Offline mode"):
        run(_call(remote, "health"))
    assert run(_call(provider(fake, offline_mode=True), "health")).version


def test_helpers() -> None:
    assert strip_think("<think>a\nb</think> Hi") == "Hi"
    assert strip_think("<think>never closed") == ""
    f = ThinkFilter()
    assert f.feed("a<") + f.feed("x") + f.flush() == "a<x"
    assert is_loopback_host("http://localhost:11434") and is_loopback_host("http://127.0.0.1:1")
    assert is_loopback_host("http://[::1]:11434") and not is_loopback_host("http://10.0.0.2:11434")
