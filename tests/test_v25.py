"""Jarvis 2.5: keywords are commands (apps, sites, Settings pages), Windows Update, and one "jarvis" model."""

from __future__ import annotations

import asyncio

import pytest

from app.agent.shortcuts import match_shortcut
from app.ai.base import ModelInfo
from app.config.settings import Settings
from app.tools.base import ToolContext, ToolError

APPS = {"google chrome": "Google Chrome", "chrome": "Google Chrome", "notepad": "Notepad", "spotify": "Spotify",
        "settings": "Windows Settings"}


def known(name):
    return APPS.get(name.lower())


def site(name):
    return name.lower() in ("youtube", "facebook", "gmail", "github")


@pytest.mark.parametrize("said, tool, arguments", [
    ("youtube", "open_website", {"site": "youtube"}),
    ("YouTube.", "open_website", {"site": "youtube"}),
    ("Jarvis, google chrome please", "open_application", {"name": "google chrome"}),
    ("spotify", "open_application", {"name": "spotify"}),
    ("bluetooth", "open_settings_page", {"page": "bluetooth"}),
    ("open bluetooth", "open_settings_page", {"page": "bluetooth"}),
    ("display settings", "open_settings_page", {"page": "display"}),
    ("device manager", "open_settings_page", {"page": "device manager"}),
    ("dark mode", "open_settings_page", {"page": "dark mode"}),
    ("scan windows update", "windows_update", {"action": "install"}),
    ("update windows", "windows_update", {"action": "install"}),
    ("check for updates", "windows_update", {"action": "install"}),
    ("windows upgrade", "windows_update", {"action": "install"}),
    ("উইন্ডোজ আপডেট করো", "windows_update", {"action": "install"}),
    ("which updates are waiting?", "windows_update", {"action": "list"}),
    ("screenshot", "take_screenshot", {}),
    ("mute", "volume_control", {"action": "mute"}),
    ("news", "get_news", {}),
])
def test_a_keyword_is_enough(said, tool, arguments) -> None:
    call = match_shortcut(said, known, known_site=site)
    assert call is not None and (call.name, dict(call.arguments)) == (tool, arguments)


@pytest.mark.parametrize("said", ["hello", "thank you", "how are you", "what is the capital of france",
                                  "tell me a joke about computers", "it"])
def test_sentences_still_go_to_the_ai(said) -> None:
    assert match_shortcut(said, known, known_site=site) is None


def test_settings_pages_are_fixed_addresses() -> None:
    from app.tools.windows_settings import PAGES, TOOLS_APPS, find_page

    assert find_page("bluetooth") == ("page", "Bluetooth & devices", "ms-settings:bluetooth")
    assert find_page("open the wallpaper settings")[2] == "ms-settings:personalization-background"
    assert find_page("blutooth")[1] == "Bluetooth & devices"  # sound-alike
    assert find_page("rm -rf everything") is None
    assert all(address.startswith(("ms-settings:", "windowsdefender:")) for address, _ in PAGES.values())
    assert all(program.endswith((".msc", ".exe", ":")) and "\\" not in program for program, _ in TOOLS_APPS.values())


def test_open_settings_page(monkeypatch) -> None:
    from app.tools import windows_settings, winapi

    opened: list[str] = []
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "start_file", opened.append)
    ctx = ToolContext(Settings())
    assert windows_settings.open_settings_page(windows_settings.PageArgs(page="display"), ctx).message == "Opened Display."
    windows_settings.open_settings_page(windows_settings.PageArgs(page="device manager"), ctx)
    assert opened[0] == "ms-settings:display" and opened[1].endswith("devmgmt.msc")
    with pytest.raises(ToolError, match="don't know"):
        windows_settings.open_settings_page(windows_settings.PageArgs(page="quantum flux"), ctx)


def test_windows_update(monkeypatch) -> None:
    from app.tools import windows_settings, winapi

    opened: list[str] = []
    started: list[int] = []
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "start_file", opened.append)
    monkeypatch.setattr(winapi, "start_windows_update", lambda: started.append(1))
    ctx = ToolContext(Settings())
    result = windows_settings.windows_update(windows_settings.UpdateArgs(action="install"), ctx)
    assert opened == ["ms-settings:windowsupdate-action"] and started == [1] and result.data["started"]
    assert "won't restart" in result.message
    windows_settings.windows_update(windows_settings.UpdateArgs(action="check"), ctx)
    assert started == [1]  # check only looks
    monkeypatch.setattr(winapi, "pending_updates", lambda: ["2026-09 Cumulative Update", "Defender definitions"])
    listing = windows_settings.windows_update(windows_settings.UpdateArgs(action="list"), ctx)
    assert listing.message.startswith("2 update(s) waiting")
    tool = next(t for t in windows_settings.TOOLS if t.name == "windows_update")
    assert tool.direct_without_confirmation(windows_settings.UpdateArgs(), ctx)  # your own command: no question
    assert int(tool.level) == 1  # ... but the AI deciding on its own still asks


# ------------------------------------------------------------------ the "jarvis" model


def model(name, size=1, caps=("completion",)):
    return ModelInfo(name=name, size_bytes=size, capabilities=frozenset(caps))


MODELS = [model("qwen3:4b-instruct", 2500), model("llama3.2:3b", 2000), model("qwen3:8b", 5200),
          model("nomic-embed-text:latest", 270, ("embedding",)), model("qwen2.5vl:3b", 3200, ("completion", "vision"))]


def test_choose_base_and_cleanup_plan() -> None:
    from app.ai import jarvis_model

    assert jarvis_model.choose_base(MODELS, "qwen3:8b").name == "qwen3:8b"
    assert jarvis_model.choose_base(MODELS, "").name == "qwen3:4b-instruct"  # a known good one
    assert jarvis_model.choose_base(MODELS, "jarvis").name == "qwen3:4b-instruct"
    assert jarvis_model.choose_base([MODELS[3]], "") is None
    plan = jarvis_model.cleanup_plan(MODELS + [model("jarvis:latest", 2500)], keep_names=("llama3.2:3b",))
    assert set(plan.delete) == {"qwen3:4b-instruct", "qwen3:8b"}
    assert {"jarvis:latest", "nomic-embed-text:latest", "qwen2.5vl:3b", "llama3.2:3b"} == set(plan.keep)


def test_build_and_delete() -> None:
    from app.ai import jarvis_model

    calls: list[tuple] = []

    class Provider:
        async def create(self, name, base, system, parameters):
            calls.append(("create", name, base, parameters))

        async def delete(self, name):
            calls.append(("delete", name))

    class Store(dict):
        def set(self, key, value):
            self[key] = value

    class AI:
        provider, store = Provider(), Store()

        async def refresh_models(self):
            return MODELS

        async def set_model(self, name):
            calls.append(("use", name))

    ai = AI()
    settings = Settings(ai_context_window=8192, ai_temperature=0.6)
    assert asyncio.run(jarvis_model.build(ai, "qwen3:4b-instruct", settings)) == "jarvis"
    assert calls[0] == ("create", "jarvis", "qwen3:4b-instruct", {"num_ctx": 8192, "temperature": 0.6})
    assert calls[1] == ("use", "jarvis") and ai.store[jarvis_model.BASE_KEY] == "qwen3:4b-instruct"
    removed = asyncio.run(jarvis_model.delete_models(ai, ["llama3.2:3b", "jarvis"]))
    assert removed == ["llama3.2:3b"] and ("delete", "jarvis") not in calls  # never deletes itself


def test_ollama_create_and_delete_requests() -> None:
    import json

    import httpx

    from app.ai.ollama import OllamaProvider

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"status": "success"}) if request.url.path == "/api/create" \
            else httpx.Response(200)

    provider = OllamaProvider("http://127.0.0.1:11434", transport=httpx.MockTransport(handler)) \
        if "transport" in OllamaProvider.__init__.__code__.co_varnames else None
    if provider is None:
        pytest.skip("provider takes no transport here")

    async def go():
        try:
            await provider.create("jarvis", "qwen3:4b-instruct", "You are Jarvis.", {"num_ctx": 4096})
            await provider.delete("llama3.2:3b")
        finally:
            await provider.aclose()

    asyncio.run(go())
    assert seen[0][:2] == ("POST", "/api/create") and seen[0][2]["from"] == "qwen3:4b-instruct"
    assert seen[0][2]["parameters"] == {"num_ctx": 4096} and seen[1] == ("DELETE", "/api/delete", {"model": "llama3.2:3b"})


# ------------------------------------------------------------------ noise is not a request


@pytest.mark.parametrize("text, seconds", [
    ("Microsoft Word Document MSWordDoc Word.", 2.0), ("Subtitles by the Amara.org community", 3.0),
    ("Thank you for watching!", 2.0), ("word word word word word word", 3.0),
    ("I don't know what to say about this one really.", 0.41),  # 39+ characters in 0.4 s: made up
])
def test_whisper_noise_is_dropped(text, seconds) -> None:
    from app.voice.speech_to_text import is_hallucination

    assert is_hallucination(text, 0.05, seconds)


@pytest.mark.parametrize("text, seconds", [("open chrome", 0.8), ("what's the weather", 1.0), ("youtube", 0.5),
                                           ("scan windows update", 1.2), ("ক্রোম খোলো", 0.9)])
def test_real_commands_are_kept(text, seconds) -> None:
    from app.voice.speech_to_text import is_hallucination

    assert not is_hallucination(text, 0.05, seconds)


def test_the_end_of_its_own_answer_is_an_echo() -> None:
    from app.agent.language import is_echo

    assert is_echo("questions.", "Got it, sir! Any other requests or questions? I'm all ears!")
    assert not is_echo("open youtube", "Got it, sir! Any other requests or questions?")


def test_a_name_with_extra_words_is_not_a_bare_keyword() -> None:
    from app.agent.shortcuts import match_keyword

    loose = lambda name: "Google Chrome" if "chrome" in name.lower() else None  # noqa: E731 - a fuzzy app finder
    assert match_keyword("google chrome club", loose) is None  # "Google Chrome খোলো" misheard: handled elsewhere
    assert match_keyword("google chrome", loose).arguments == {"name": "google chrome"}
    assert match_keyword("chrome", loose).arguments == {"name": "chrome"}
