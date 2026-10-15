from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.agent.conversation import ConversationManager, parse_text_tool_call
from app.agent.tool_manager import ToolExecutor, ToolRegistry
from app.ai.manager import AIManager
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.security.permissions import PermissionEngine, RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult
from app.tools.catalog import build_default_registry
from tests.fake_ollama import FakeOllama, model_entry
from tests.test_tools import PathArgs, ScriptedConfirmer

NOW = datetime(2026, 9, 23, 13, 2, tzinfo=timezone(timedelta(hours=6)))


def call(name: str, **args) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def say(text: str) -> dict:
    return {"role": "assistant", "content": text}


def build(replies: list[dict], registry: ToolRegistry | None = None, confirm: bool = True, stream_parts=None):
    fake = FakeOllama(models=[model_entry("llama3.2:3b")], replies=list(replies), stream_parts=stream_parts)
    settings = Settings()
    executions = []
    ex = ToolExecutor(
        registry or build_default_registry(), PermissionEngine(PermissionsConfig()),
        ToolContext(settings, clock=lambda: NOW), confirmer=ScriptedConfirmer(confirm),
    )
    conv = ConversationManager(
        AIManager.from_settings(settings, transport=fake.transport()), settings,
        clock=lambda: NOW, tools=ex, on_tool=executions.append,
    )
    return conv, fake, executions


def run(conv: ConversationManager, text: str, stream: bool = False):
    async def go():
        pieces: list[str] = []
        try:
            reply = await conv.send(text, on_delta=pieces.append if stream else None)
            return reply, "".join(pieces)
        finally:
            await conv.ai.aclose()

    return asyncio.run(go())


def chat_bodies(fake: FakeOllama) -> list[dict]:
    return [body for _, path, body in fake.requests if path == "/api/chat"]


def test_tool_call_then_answer() -> None:
    conv, fake, executions = build([call("get_datetime"), say("It's 13:02.")])
    reply, _ = run(conv, "Could you check the clock for me?")
    assert reply.ok and reply.text == "It's 13:02."
    assert [e.call.name for e in reply.tools_used] == ["get_datetime"] and len(executions) == 1
    second = chat_bodies(fake)[1]
    assert second["messages"][-1]["role"] == "tool" and '"time_24h": "13:02"' in second["messages"][-1]["content"]
    assert [t["function"]["name"] for t in second["tools"]][:2] == ["get_datetime", "calculate"]
    # the whole turn (incl. the tool exchange) is kept together in the context
    assert [m.role for m in conv.context.messages] == ["user", "assistant", "tool", "assistant"]


def test_streaming_with_tools() -> None:
    conv, _, _ = build([call("calculate", expression="12*7"), say("12 times 7 is 84.")])
    reply, streamed = run(conv, "12 times 7?", stream=True)
    assert reply.text == "12 times 7 is 84." and streamed == "12 times 7 is 84."


def test_json_written_as_text_is_recovered_and_not_spoken() -> None:
    text_call = say('{"name": "get_datetime", "parameters": {}}')
    conv, _, executions = build([text_call, say("It is 1:02 PM.")])
    reply, streamed = run(conv, "the hour, please?", stream=True)
    assert len(executions) == 1 and reply.text == "It is 1:02 PM."
    assert "{" not in streamed and streamed == "It is 1:02 PM."


def test_ordinary_braces_text_is_still_shown() -> None:
    conv, _, executions = build([say("{curly} is just a word here.")])
    reply, streamed = run(conv, "hi", stream=True)
    assert not executions and streamed == "{curly} is just a word here."


def test_identical_calls_execute_once_and_rounds_are_limited() -> None:
    counter = {"n": 0}

    def handler(args, ctx):
        counter["n"] += 1
        return ToolResult.success("opened")

    registry = ToolRegistry([Tool("open_thing", "opens", handler, PathArgs)])
    looping = [call("open_thing", path="chrome")] * 10
    conv, fake, _ = build(looping + [say("Done.")], registry)
    reply, _ = run(conv, "open chrome")
    assert counter["n"] == 1  # never repeated
    bodies = chat_bodies(fake)
    assert len(bodies) == 5 and "tools" not in bodies[-1]  # 4 tool rounds, then a forced answer


def test_declined_confirmation_is_reported_to_model() -> None:
    ran = []
    registry = ToolRegistry([
        Tool("restart_pc", "restart", lambda a, c: ran.append(1), level=RiskLevel.DANGEROUS,
             confirmation=lambda a, c: "Restart the computer now?")
    ])
    conv, fake, _ = build([call("restart_pc"), say("Okay, I won't restart.")], registry, confirm=False)
    reply, _ = run(conv, "restart my computer")
    assert not ran and reply.text == "Okay, I won't restart."
    tool_msg = chat_bodies(fake)[1]["messages"][-1]
    assert json.loads(tool_msg["content"])["error"] == "declined"


def test_unknown_tool_from_model_is_handled() -> None:
    conv, fake, _ = build([call("delete_everything"), say("I can't do that.")])
    reply, _ = run(conv, "delete everything")
    assert reply.ok and json.loads(chat_bodies(fake)[1]["messages"][-1]["content"])["error"] == "unknown_tool"


def test_model_without_tool_support_falls_back_to_plain_chat() -> None:
    fake = FakeOllama(models=[model_entry("gemma:2b")], capabilities={"gemma:2b": ["completion"]}, replies=[say("Hi!")])
    settings = Settings()
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ToolContext(settings))
    conv = ConversationManager(AIManager.from_settings(settings, transport=fake.transport()), settings, tools=ex)
    reply, _ = run(conv, "hello")
    assert reply.ok and reply.text == "Hi!" and "tools" not in chat_bodies(fake)[-1]


def test_parse_text_tool_call_variants() -> None:
    known = {"calculate"}
    assert parse_text_tool_call('{"name": "calculate", "parameters": {"expression": "1+1"}}', known).arguments == {"expression": "1+1"}
    assert parse_text_tool_call('```json\n{"name": "calculate", "arguments": {}}\n```', known) is not None
    assert parse_text_tool_call('{"function": {"name": "calculate", "arguments": {}}}', known) is not None
    assert parse_text_tool_call('{"name": "rm_rf", "parameters": {}}', known) is None
    assert parse_text_tool_call("The answer is {x}", known) is None
