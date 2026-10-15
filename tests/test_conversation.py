from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.agent.context import ConversationContext
from app.agent.conversation import ConversationManager
from app.ai.base import ChatMessage, ToolCall
from app.ai.manager import AIManager
from app.ai.prompts import build_system_prompt
from app.config.settings import Settings
from app.memory.conversations import ConversationStore
from app.memory.database import Database
from tests.fake_ollama import FakeOllama, model_entry

FIXED_NOW = datetime(2026, 9, 23, 14, 30, tzinfo=timezone.utc)


def make(fake: FakeOllama, store: ConversationStore | None = None, **settings) -> ConversationManager:
    s = Settings(**settings)
    return ConversationManager(AIManager.from_settings(s, transport=fake.transport()), s, store, clock=lambda: FIXED_NOW)


def run(conv: ConversationManager, coro_fn):
    async def _inner():
        try:
            return await coro_fn(conv)
        finally:
            await conv.ai.aclose()

    return asyncio.run(_inner())


def fake() -> FakeOllama:
    return FakeOllama(models=[model_entry("llama3.2:latest")])


def test_reply_and_context_carried_forward() -> None:
    f = fake()
    conv = make(f)

    async def two_turns(c: ConversationManager):
        first = await c.send("Open Chrome.")
        f.reply = {"role": "assistant", "content": "Opening YouTube."}
        second = await c.send("Now open YouTube.")
        return first, second

    first, second = run(conv, two_turns)
    assert first.ok and first.text == "Hello." and second.text == "Opening YouTube."
    messages = f.last_chat_payload()["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "Open Chrome." and "Wednesday, 23 September 2026" in messages[0]["content"]


def test_streaming_callback_receives_text() -> None:
    f = fake()
    f.stream_parts = ["\n\n", "Your CPU ", "is fine."]
    pieces: list[str] = []
    reply = run(make(f), lambda c: c.send("hi", on_delta=pieces.append))
    assert reply.text == "Your CPU is fine." and "".join(pieces) == "Your CPU is fine."


def test_failure_is_friendly_and_does_not_pollute_context() -> None:
    f = FakeOllama(raise_on={"/api/tags": httpx.ConnectError("refused")})
    conv = make(f)
    reply = run(conv, lambda c: c.send("hello"))
    assert not reply.ok and reply.error == "provider_unavailable"
    assert reply.text == "Ollama is not running. Start Ollama and try again."
    assert len(conv.context) == 0


def test_server_error_message_hides_details() -> None:
    f = fake()
    f.chat_status, f.chat_error = 500, "CUDA error: out of memory at 0xdeadbeef"
    reply = run(make(f), lambda c: c.send("hello"))
    assert reply.error == "request_failed" and "0xdeadbeef" not in reply.text


def test_input_validation() -> None:
    conv = make(fake())
    assert run(conv, lambda c: c.send("   ")).error == "empty_input"
    assert run(make(fake()), lambda c: c.send("x" * 9000)).error == "input_too_long"


def test_metadata_recorded(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    db.initialize()
    store = ConversationStore(db)
    conv = make(fake(), store)

    async def turns(c: ConversationManager):
        await c.send("Remember api_key=abc123 please")
        await c.send("second")

    run(conv, turns)
    row = store.get(conv.conversation_id)
    assert row["message_count"] == 4 and row["model"] == "llama3.2:latest"
    assert "abc123" not in row["title"]


def test_reset_starts_new_conversation() -> None:
    conv = make(fake())
    run(conv, lambda c: c.send("hi"))
    conv.reset()
    assert len(conv.context) == 0 and conv.conversation_id is None


def test_context_trims_whole_turns() -> None:
    ctx = ConversationContext(max_messages=4, max_chars=10_000)
    for i in range(3):
        ctx.add_turn([ChatMessage.user(f"u{i}"), ChatMessage.assistant("", [ToolCall("t", {})]), ChatMessage.tool("t", "r"), ChatMessage.assistant(f"a{i}")])
    assert [m.content for m in ctx.messages] == ["u2", "", "r", "a2"]
    ctx = ConversationContext(max_messages=100, max_chars=30)
    ctx.add_turn([ChatMessage.user("a" * 20), ChatMessage.assistant("b")])
    ctx.add_turn([ChatMessage.user("c" * 20), ChatMessage.assistant("d")])
    assert [m.content[0] for m in ctx.messages] == ["c", "d"]
    with pytest.raises(ValueError):
        ctx.add_turn([ChatMessage.assistant("no user")])


def test_prompt_rules() -> None:
    plain = build_system_prompt(assistant_name="Jarvis", now=FIXED_NOW)
    assert "cannot control the computer" in plain and "Do not use honorifics" in plain
    custom = build_system_prompt(assistant_name="Nova", now=FIXED_NOW, user_address="Labib", offline_mode=True)
    assert "You are Nova" in custom and "'Labib'" in custom and "Offline mode is on" in custom


def test_spoken_mode_prompt() -> None:
    spoken = build_system_prompt(assistant_name="Jarvis", now=FIXED_NOW, spoken=True)
    assert "read aloud" in spoken and "never read out lists" in spoken
    assert "read aloud" not in build_system_prompt(assistant_name="Jarvis", now=FIXED_NOW)
