"""3.1.4: "close everything" closes every open app (no AI needed), and the model is warmed up at the
size the chat uses (the first question took over two minutes)."""

from __future__ import annotations

import asyncio
import os

import pytest

from app.agent.shortcuts import match_shortcut
from app.tools import winapi
from tests.test_windows_tools import FakeDesktop, W, run_tool


@pytest.fixture
def desktop(monkeypatch) -> FakeDesktop:
    fake = FakeDesktop(monkeypatch)
    fake.windows += [
        W(10, 300, "main.py - Jarvis - Visual Studio Code", "Code.exe"),
        W(11, 400, "Program Manager", "explorer.exe"),  # the desktop itself
        W(12, 401, "Downloads", "explorer.exe"),  # a File Explorer folder: closed
        W(13, os.getpid(), "Jarvis - Jarvis Assistant", "python.exe"),  # Jarvis itself
        W(14, 500, "Ollama", "ollama app.exe"),
    ]
    return fake


@pytest.mark.parametrize("said, keep", [
    ("close everything", None), ("close all apps", None), ("Close all the open windows", None),
    ("quit everything please", None), ("সব বন্ধ করো", None), ("সব অ্যাপ বন্ধ করো", None),
    ("close everything except Chrome", "Chrome"), ("close all apps except chrome and spotify", "chrome and spotify"),
])
def test_close_everything_is_a_direct_command(said, keep) -> None:
    call = match_shortcut(said, lambda n: None)
    assert call is not None and call.name == "close_all_apps"
    assert call.arguments.get("keep") == keep


@pytest.mark.parametrize("said", ["close chrome", "close all tabs", "close the door"])
def test_other_close_commands_are_unchanged(said) -> None:
    call = match_shortcut(said, lambda n: "Google Chrome" if "chrome" in n else None)
    assert call is None or call.name != "close_all_apps"


def test_every_app_is_closed_but_jarvis_and_windows_stay(desktop: FakeDesktop) -> None:
    result = run_tool("close_all_apps").result
    assert result.ok
    assert sorted(desktop.closed) == [1, 2, 3, 10, 12]
    assert {11, 13, 14} <= {w.hwnd for w in desktop.windows}  # desktop, Jarvis, Ollama
    assert result.data["closed"] == ["Google Chrome", "Notepad", "Visual Studio Code", "File Explorer"]
    assert result.message.startswith("Closed 4 apps")


def test_keep_some_open(desktop: FakeDesktop) -> None:
    run_tool("close_all_apps", keep="chrome")
    assert sorted(desktop.closed) == [3, 10, 12]


def test_apps_that_ask_to_save_are_reported(desktop: FakeDesktop, monkeypatch) -> None:
    from app.tools import applications

    monkeypatch.setattr(applications, "_wait_for", lambda check, seconds, step=0.1: check())
    desktop.closes_ok = False  # every app shows "save your changes?"
    result = run_tool("close_all_apps").result
    assert result.data["closed"] == [] and "Still open (probably asking to save your work)" in result.message


def test_nothing_to_close(monkeypatch) -> None:
    fake = FakeDesktop(monkeypatch)
    fake.windows = [W(11, 400, "Program Manager", "explorer.exe")]
    assert run_tool("close_all_apps").result.message == "There are no open apps to close."
    assert winapi  # (the fake replaced its functions)


# ------------------------------------------------------------------ warm-up at the chat's size

def test_warm_up_uses_the_chat_context_size() -> None:
    from app.ai.manager import AIManager
    from app.config.settings import Settings
    from tests.fake_ollama import FakeOllama, model_entry

    fake = FakeOllama(models=[model_entry("qwen3:4b-instruct")])
    ai = AIManager.from_settings(Settings(ai_model="qwen3:4b-instruct"), transport=fake.transport())
    ai.provider.context_window = 6144  # the GPU memory saver lowered it

    async def go():
        try:
            await ai.warm_up()
        finally:
            await ai.aclose()

    asyncio.run(go())
    body = next(b for _, path, b in fake.requests if path == "/api/generate")
    assert body["options"]["num_ctx"] == 6144  # same as the chat requests: Ollama doesn't reload the model
