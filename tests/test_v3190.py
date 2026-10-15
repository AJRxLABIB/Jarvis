"""3.19.0: model router 2.0 - coding route, sensitive requests local-only, fallback when a model fails,
and "which model are you using"."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.agent.conversation import ConversationManager
from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecutor
from app.ai.base import AIError, ChatMessage, ToolCall
from app.ai.manager import AIManager
from app.ai.router import is_local_host, route
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import modes
from app.security.permissions import PermissionEngine
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry
from tests.fake_ollama import FakeOllama, model_entry


class Failing(FakeOllama):
    """A fake Ollama where one model's chat requests fail."""

    broken: str = ""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat" and self.broken and self.broken.encode() in request.content:
            self.requests.append(("POST", "/api/chat", None))
            return httpx.Response(500, json={"error": "model crashed"})
        return super().handle(request)


def models(*names):
    return [model_entry(n) for n in names]


@pytest.mark.parametrize(("text", "kind"), [
    ("why does my python function throw a KeyError", "coding"),
    ("the tests are failing after my last commit", "coding"),
    ("open VS Code", "normal"),  # a command, not a coding question (the word "code" isn't coding)
    ("explain the difference between TCP and UDP in detail", "smart"),
    ("what time is it", "fast"),
])
def test_routes(text, kind) -> None:
    from app.agent.conversation import is_hard_request, is_simple_request, is_task

    decision = route(text, simple=is_simple_request(text), hard=is_hard_request(text), task=is_task(text))
    assert decision.kind == kind, decision


def test_fix_it_in_an_editor_is_a_coding_follow_up() -> None:
    assert route("fix it", simple=True, hard=False, task=False, in_editor=True).kind == "coding"
    assert route("fix it", simple=True, hard=False, task=False, in_editor=False).kind == "fast"


def test_sensitive_requests() -> None:
    assert route("what's my bank password hint", simple=True, hard=False, task=False).sensitive
    assert route("hello", simple=True, hard=False, task=False, private_mode=True).sensitive
    assert not route("hello", simple=True, hard=False, task=False).sensitive


@pytest.mark.parametrize(("url", "local"), [("http://127.0.0.1:11434", True), ("http://localhost:11434", True),
                                            ("http://192.168.1.20:11434", True), ("http://100.101.1.2:11434", True),
                                            ("http://8.8.8.8:11434", False)])
def test_local_hosts(url, local) -> None:
    assert is_local_host(url) is local


def _manager(fake, **settings) -> AIManager:
    return AIManager.from_settings(Settings(**settings), transport=fake.transport())


def test_coding_model_is_used_and_falls_back_to_smart_then_main() -> None:
    fake = FakeOllama(models=models("qwen3:4b-instruct", "qwen2.5-coder:7b", "qwen3:8b"))
    ai = _manager(fake, ai_model="qwen3:4b-instruct", ai_coding_model="qwen2.5-coder:7b", ai_smart_model="qwen3:8b")
    asyncio.run(ai.chat([ChatMessage.user("fix the bug")], coding=True))
    assert fake.last_chat_payload()["model"] == "qwen2.5-coder:7b" and ai.last_route["role"] == "coding"
    fake2 = FakeOllama(models=models("qwen3:4b-instruct", "qwen3:8b"))
    ai2 = _manager(fake2, ai_model="qwen3:4b-instruct", ai_coding_model="qwen2.5-coder:7b", ai_smart_model="qwen3:8b")
    asyncio.run(ai2.chat([ChatMessage.user("fix the bug")], coding=True))
    assert fake2.last_chat_payload()["model"] == "qwen3:8b"  # coder not installed -> smart


def test_a_failing_model_falls_back_to_the_main_model_once() -> None:
    fake = Failing(models=models("qwen3:4b-instruct", "qwen3:8b"))
    fake.broken = "qwen3:8b"
    ai = _manager(fake, ai_model="qwen3:4b-instruct", ai_smart_model="qwen3:8b")
    response = asyncio.run(ai.chat([ChatMessage.user("explain it")], smart=True))
    assert response.model == "qwen3:4b-instruct" and "qwen3:8b failed" in ai.last_route["fallback"]

    async def stream():
        return [model async for model, _ in ai.stream_chat([ChatMessage.user("explain it")], smart=True)]

    assert set(asyncio.run(stream())) == {"qwen3:4b-instruct"}


def test_the_main_model_failing_is_not_hidden() -> None:
    fake = Failing(models=models("qwen3:4b-instruct"))
    fake.broken = "qwen3:4b-instruct"
    ai = _manager(fake, ai_model="qwen3:4b-instruct")
    with pytest.raises(AIError):
        asyncio.run(ai.chat([ChatMessage.user("hello")]))


def test_private_requests_never_go_to_an_internet_ollama() -> None:
    fake = FakeOllama(models=models("qwen3:4b-instruct"))
    ai = _manager(fake, ai_model="qwen3:4b-instruct", ollama_host="http://8.8.8.8:11434")
    with pytest.raises(AIError) as caught:
        asyncio.run(ai.chat([ChatMessage.user("my bank")], sensitive=True))
    assert "won't send it there" in str(caught.value)
    assert not any(path == "/api/chat" for _, path, _ in fake.requests)


def test_conversation_uses_the_coding_route() -> None:
    fake = FakeOllama(models=models("qwen3:4b-instruct", "qwen2.5-coder:7b"))
    settings = Settings(ai_model="qwen3:4b-instruct", ai_coding_model="qwen2.5-coder:7b")
    conversation = ConversationManager(AIManager.from_settings(settings, transport=fake.transport()), settings)
    reply = asyncio.run(conversation.send("why does my javascript function return undefined"))
    assert reply.ok and fake.last_chat_payload()["model"] == "qwen2.5-coder:7b"
    assert conversation.last_route.kind == "coding"
    asyncio.run(conversation.send("hello there"))
    assert fake.last_chat_payload()["model"] == "qwen3:4b-instruct"


def test_private_mode_marks_everything_sensitive() -> None:
    fake = FakeOllama(models=models("qwen3:4b-instruct"))
    settings = Settings(ai_model="qwen3:4b-instruct", ollama_host="http://8.8.8.8:11434")
    conversation = ConversationManager(AIManager.from_settings(settings, transport=fake.transport()), settings)
    modes.STATE.current = modes.MODES["private"]
    reply = asyncio.run(conversation.send("hello there"))
    assert not reply.ok and not any(path == "/api/chat" for _, path, _ in fake.requests)


def test_model_status_tool_and_shortcut() -> None:
    fake = FakeOllama(models=models("qwen3:4b-instruct"))
    settings = Settings(ai_model="qwen3:4b-instruct", ai_coding_model="qwen2.5-coder:7b")
    ai = AIManager.from_settings(settings, transport=fake.transport())
    ctx = ToolContext(settings, ai=ai)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx)
    result = asyncio.run(ex.execute(ToolCall("model_status", {}))).result
    assert result.ok and "coding: qwen2.5-coder:7b (NOT installed" in result.message
    assert "on this PC or your network" in result.message and result.data["installed"] == ["qwen3:4b-instruct"]
    assert match_shortcut("which model are you using", lambda n: None).name == "model_status"
