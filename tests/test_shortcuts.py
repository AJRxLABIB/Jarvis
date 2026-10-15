from __future__ import annotations

import asyncio

import pytest

from app.agent.conversation import ConversationManager
from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecutor
from app.ai.manager import AIManager
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.security.permissions import PermissionEngine
from app.tools import applications
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry
from tests.fake_ollama import FakeOllama, model_entry
from tests.test_windows_tools import FakeDesktop  # noqa: F401  (fixture helpers)

KNOWN = {"whatsapp": "WhatsApp", "chrome": "Google Chrome", "google chrome": "Google Chrome", "vs code": "Visual Studio Code"}


def known(name: str):
    return KNOWN.get(name.lower())


@pytest.mark.parametrize(
    ("text", "tool", "name"),
    [
        ("open WhatsApp", "open_application", "WhatsApp"),
        ("Close WhatsApp.", "close_application", "WhatsApp"),
        ("please close the whatsapp app", "close_application", "whatsapp"),
        ("can you open google chrome please", "open_application", "google chrome"),
        ("launch VS Code", "open_application", "VS Code"),
        ("quit chrome!", "close_application", "chrome"),
    ],
)
def test_shortcuts_match_known_apps(text: str, tool: str, name: str) -> None:
    call = match_shortcut(text, known)
    assert call is not None and call.name == tool and call.arguments == {"name": name}


@pytest.mark.parametrize(
    "text",
    ["close part step", "close bar set", "open my downloads folder", "open report.pdf",
     "open youtube website", "close it", "open WhatsApp and send a message to my mother saying hello there",
     "what is open", "open"],
)
def test_everything_else_goes_to_the_model(text: str) -> None:
    assert match_shortcut(text, known) is None


def build(desktop_monkeypatch, direct: bool = True):
    fake = FakeOllama(models=[model_entry("llama3.2:3b")], reply={"role": "assistant", "content": "from the model"})
    settings = Settings(direct_commands=direct)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ToolContext(settings))
    conv = ConversationManager(AIManager.from_settings(settings, transport=fake.transport()), settings, tools=ex)
    return conv, fake


def run(conv, text):
    async def go():
        try:
            return await conv.send(text)
        finally:
            await conv.ai.aclose()
    return asyncio.run(go())


def test_direct_close_skips_the_model(monkeypatch) -> None:
    desktop = FakeDesktop(monkeypatch)
    desktop.windows = [applications.winapi.WindowInfo(20, 900, "WhatsApp", "whatsapp.root.exe")]
    monkeypatch.setattr(applications, "known_application", lambda name, ctx: known(name))
    conv, fake = build(monkeypatch)
    reply = run(conv, "close WhatsApp")
    assert reply.ok and reply.text == "Closed WhatsApp." and desktop.closed == [20]
    assert not [r for r in fake.requests if r[1] == "/api/chat"]  # the model was never asked
    assert [m.role for m in conv.context.messages] == ["user", "assistant", "tool", "assistant"]


def test_misheard_name_goes_to_the_model(monkeypatch) -> None:
    FakeDesktop(monkeypatch)
    monkeypatch.setattr(applications, "known_application", lambda name, ctx: known(name))
    conv, fake = build(monkeypatch)
    reply = run(conv, "close part step")
    assert reply.text == "from the model" and [r for r in fake.requests if r[1] == "/api/chat"]


def test_direct_commands_can_be_turned_off(monkeypatch) -> None:
    FakeDesktop(monkeypatch)
    monkeypatch.setattr(applications, "known_application", lambda name, ctx: known(name))
    conv, fake = build(monkeypatch, direct=False)
    assert run(conv, "close WhatsApp").text == "from the model"


def test_minimize_all_with_invented_target_is_refused(monkeypatch) -> None:
    from tests.test_windows_tools import run_tool

    desktop = FakeDesktop(monkeypatch)
    result = run_tool("window_control", action="minimize_all", name="doorstep").result
    assert result.error == "ambiguous" and (0, 0) not in desktop.shown
    assert run_tool("window_control", action="minimize_all").result.ok
