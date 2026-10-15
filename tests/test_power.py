"""Restart / shutdown: your own direct command needs no dialog (if you choose), the AI's always does;
a countdown you can cancel; 'save and restart' saves first."""

from __future__ import annotations

import asyncio

import pytest

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.security.permissions import PermissionEngine
from app.tools import windows as power
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry
from app.tools.winapi import WindowInfo
from tests.test_tools import ScriptedConfirmer
from tests.test_windows_tools import FakeDesktop


@pytest.fixture()
def desktop(monkeypatch) -> FakeDesktop:
    return FakeDesktop(monkeypatch)


def run(call: ToolCall, *, confirm: bool, user_command: bool, **settings):
    confirmer = ScriptedConfirmer(confirm)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ToolContext(Settings(**settings)),
                      confirmer=confirmer)
    return asyncio.run(ex.execute(call, user_command=user_command)).result, confirmer


RESTART = ToolCall("power_control", {"action": "restart", "delay_seconds": 10})


def test_your_own_command_needs_no_dialog_when_turned_off(desktop: FakeDesktop) -> None:
    result, confirmer = run(RESTART, confirm=False, user_command=True, confirm_power_actions=False)
    assert result.ok and confirmer.requests == [] and "Restarting in 10 seconds" in result.message
    assert desktop.timers[0][0] == 10 and power.power_pending() == "restart"
    power.cancel_pending_power()


def test_the_ai_always_asks(desktop: FakeDesktop) -> None:
    """A model's tool call (maybe prompted by a web page) never skips the dialog."""
    result, confirmer = run(RESTART, confirm=False, user_command=False, confirm_power_actions=False)
    assert result.error == "declined" and len(confirmer.requests) == 1 and desktop.timers == []


def test_default_still_asks_for_your_command(desktop: FakeDesktop) -> None:
    result, confirmer = run(RESTART, confirm=False, user_command=True)
    assert result.error == "declined" and confirmer.requests


def test_sign_out_always_asks(desktop: FakeDesktop) -> None:
    result, confirmer = run(ToolCall("power_control", {"action": "sign_out"}), confirm=False, user_command=True,
                            confirm_power_actions=False)
    assert result.error == "declined" and confirmer.requests


def test_countdown_then_graceful_restart(desktop: FakeDesktop) -> None:
    run(RESTART, confirm=True, user_command=True, confirm_power_actions=False)
    delay, timer = desktop.timers[0]
    assert desktop.shutdowns == []  # nothing yet: the countdown is running
    timer.fire()
    assert desktop.shutdowns == [("restart", 0)] and power.power_pending() is None


def test_esc_cancels_the_countdown(desktop: FakeDesktop) -> None:
    run(RESTART, confirm=True, user_command=True, confirm_power_actions=False)
    assert power.cancel_pending_power()
    desktop.timers[0][1].fire()
    assert desktop.shutdowns == [] and not power.cancel_pending_power()


def test_save_and_restart(desktop: FakeDesktop, monkeypatch) -> None:
    titles = {5: "● main.py - Jarvis - Visual Studio Code", 6: "*notes.txt - Notepad", 7: "Inbox - Outlook"}
    monkeypatch.setattr(power.winapi, "visible_windows",
                        lambda: [WindowInfo(h, 10 + h, t, "x.exe") for h, t in titles.items()])
    pressed = []

    def ctrl_s():
        hwnd = desktop.focused[-1]
        pressed.append(hwnd)
        titles[hwnd] = titles[hwnd].replace("● ", "").lstrip("*")  # the app saved

    monkeypatch.setattr(power.winapi, "press_ctrl_s", ctrl_s)
    monkeypatch.setattr(power.winapi, "window_title", lambda hwnd: titles[hwnd])
    monkeypatch.setattr(power.time, "sleep", lambda s: None)
    call = ToolCall("power_control", {"action": "restart", "delay_seconds": 10, "save_work": True})
    result, _ = run(call, confirm=True, user_command=True, confirm_power_actions=False)
    assert result.ok and pressed == [5, 6] and "Saved 2 window(s) first" in result.message
    power.cancel_pending_power()


def test_untitled_work_stops_the_restart(desktop: FakeDesktop, monkeypatch) -> None:
    monkeypatch.setattr(power.winapi, "visible_windows",
                        lambda: [WindowInfo(9, 19, "*Untitled - Paint", "mspaint.exe")])
    monkeypatch.setattr(power.winapi, "press_ctrl_s", lambda: pytest.fail("never pressed for an untitled file"))
    call = ToolCall("power_control", {"action": "shutdown", "delay_seconds": 10, "save_work": True})
    result, _ = run(call, confirm=True, user_command=True, confirm_power_actions=False)
    assert result.error == "unsaved_work" and "Untitled - Paint" in result.message
    assert desktop.timers == [] and desktop.shutdowns == []


def test_placeholder_delay_uses_the_default(desktop: FakeDesktop) -> None:
    """The model once sent delay_seconds='null' and the call was rejected."""
    result, _ = run(ToolCall("power_control", {"action": "restart", "delay_seconds": "null"}), confirm=True,
                    user_command=False)
    assert result.ok and desktop.timers[0][0] == 10
    power.cancel_pending_power()


def test_direct_commands_are_marked_as_yours(monkeypatch) -> None:
    """The conversation runs matched commands with user_command=True; the model's calls never are."""
    from app.agent.conversation import ConversationManager

    seen = []

    class Tools:
        registry = build_default_registry()
        context = ToolContext(Settings())

        async def execute(self, call, user_command=False):
            seen.append((call.name, user_command))
            from app.agent.tool_manager import ToolExecution
            from app.tools.base import ToolResult
            return ToolExecution(call, ToolResult.success("Restarting in 10 seconds."), None, None, 1)

    conversation = ConversationManager(None, Settings(), None, tools=Tools())
    reply = asyncio.run(conversation.send("Restart my PC."))
    assert reply.ok and seen == [("power_control", True)]



def test_plain_restart_saves_work_first(desktop: FakeDesktop, monkeypatch) -> None:
    """SAVE_BEFORE_POWER (default on): "restart my PC" saves open work without being told."""
    titles = {5: "● main.py - Jarvis - Visual Studio Code"}
    monkeypatch.setattr(power.winapi, "visible_windows", lambda: [WindowInfo(5, 15, titles[5], "code.exe")])
    pressed = []

    def ctrl_s():
        pressed.append(1)
        titles[5] = "main.py - Jarvis - Visual Studio Code"

    monkeypatch.setattr(power.winapi, "press_ctrl_s", ctrl_s)
    monkeypatch.setattr(power.winapi, "window_title", lambda hwnd: titles[hwnd])
    monkeypatch.setattr(power.time, "sleep", lambda s: None)
    result, confirmer = run(RESTART, confirm=False, user_command=True, confirm_power_actions=False)
    assert result.ok and pressed == [1] and "Saved 1 window(s) first" in result.message and not confirmer.requests
    power.cancel_pending_power()


def test_untitled_tabs_in_notepad_or_vs_code_do_not_block(desktop: FakeDesktop, monkeypatch) -> None:
    """They bring back unsaved tabs after a restart by themselves."""
    monkeypatch.setattr(power.winapi, "visible_windows", lambda: [
        WindowInfo(9, 19, "*Untitled - Notepad", "Notepad.exe"), WindowInfo(8, 18, "● Untitled-1 - Visual Studio Code",
                                                                           "Code.exe")])
    monkeypatch.setattr(power.winapi, "press_ctrl_s", lambda: pytest.fail("nothing to press"))
    result, _ = run(RESTART, confirm=True, user_command=True, confirm_power_actions=False)
    assert result.ok and desktop.timers
    power.cancel_pending_power()


@pytest.mark.parametrize(("said", "action", "counts"), [
    ("Restart my computer bomb.", "restart", True),  # odd speech recognition, still clearly "restart"
    ("please shut down the laptop now", "shutdown", True),
    ("turn off my pc", "shutdown", True),
    ("summarise this web page", "restart", False),  # a web page asked the model to restart: not you
    ("restart chrome", "shutdown", False),
])
def test_what_counts_as_your_request(said: str, action: str, counts: bool) -> None:
    from app.agent.conversation import user_asked_for

    assert user_asked_for(ToolCall("power_control", {"action": action}), said) is counts
    assert user_asked_for(ToolCall("delete_item", {"path": "x"}), "restart and delete x") is False


def test_model_call_for_your_own_restart_skips_the_dialog(desktop: FakeDesktop) -> None:
    """The conversation marks the model's restart as yours only when your words asked for it."""
    import httpx  # noqa: F401  (the fake Ollama below uses it)

    from app.agent.conversation import ConversationManager
    from app.ai.manager import AIManager
    from tests.fake_ollama import FakeOllama, model_entry

    fake = FakeOllama(models=[model_entry("llama3.2:3b")], replies=[
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "power_control",
                                                                          "arguments": {"action": "restart"}}}]},
        {"role": "assistant", "content": "Restarting."}])
    settings = Settings(confirm_power_actions=False, direct_commands=False)
    confirmer = ScriptedConfirmer(False)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ToolContext(settings),
                      confirmer=confirmer)
    conversation = ConversationManager(AIManager.from_settings(settings, transport=fake.transport()), settings, None,
                                       tools=ex)
    reply = asyncio.run(conversation.send("Restart my computer bomb."))
    assert reply.ok and confirmer.requests == [] and desktop.timers
    power.cancel_pending_power()
