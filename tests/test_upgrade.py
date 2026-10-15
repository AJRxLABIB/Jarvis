"""Voice recognition, smarter direct commands and session context (the v1.1 upgrade)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.agent.conversation import ConversationManager, resolve_reference
from app.agent.shortcuts import match_shortcut, split_commands
from app.agent.tool_manager import ToolExecutor, ToolRegistry
from app.ai.manager import AIManager
from app.ai.prompts import build_system_prompt
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.security.permissions import PermissionEngine
from app.tools.applications import AppNameArgs, best_match, running_app_names
from app.tools.base import Tool, ToolContext, ToolResult
from app.tools.winapi import WindowInfo
from app.utils.phonetic import best_sounding, similarity
from app.voice.speech_to_text import FasterWhisperSTT, STTError, normalize_level
from app.voice.stt_select import ModelChoice, is_cuda_error, stt_candidates
from app.voice.vad import EnergyVAD, SileroVAD, create_vad, silero_model_path
from tests.fake_ollama import FakeOllama, model_entry
from tests.test_agent_loop import NOW, say
from tests.test_stt import FakeWhisper, seg
from tests.test_tools import ScriptedConfirmer

# ------------------------------------------------------------------ choosing the speech model

def speech(n: int = 16000):
    """Audible test audio (pure silence is skipped before recognition)."""
    return (0.1 * np.sin(np.arange(n) / 3)).astype(np.float32)



def test_auto_prefers_the_gpu_turbo_model_with_cpu_fallbacks() -> None:
    s = Settings()
    assert (s.stt_model, s.stt_device, s.stt_compute_type) == ("auto", "auto", "auto")
    assert stt_candidates(s, gpu=True) == [
        ModelChoice("large-v3-turbo", "cuda", "int8_float16"), ModelChoice("small.en", "cpu", "int8"),
        ModelChoice("base.en", "cpu", "int8")]
    assert stt_candidates(s, gpu=False) == [ModelChoice("small.en", "cpu", "int8"), ModelChoice("base.en", "cpu", "int8")]
    multi = Settings(voice_language="bn")  # "auto" allows any language
    assert [c.model for c in stt_candidates(multi, gpu=False)] == ["small", "base"]


def test_explicit_model_and_device() -> None:
    assert stt_candidates(Settings(stt_model="base.en", stt_device="cpu", stt_compute_type="int8"), gpu=True) == [
        ModelChoice("base.en", "cpu", "int8")]
    cuda = stt_candidates(Settings(stt_model="medium.en", stt_device="cuda"), gpu=False)
    assert cuda[0] == ModelChoice("medium.en", "cuda", "int8_float16") and cuda[1].device == "cpu"
    assert is_cuda_error(RuntimeError("Library cublas64_12.dll is not found or cannot be loaded"))
    assert not is_cuda_error(RuntimeError("disk full"))


def test_gpu_without_its_libraries_is_not_used(monkeypatch) -> None:
    from app.voice import stt_select

    def missing(name):
        raise OSError(f"{name} not found")

    assert stt_select.cuda_libraries_present(loader=missing) is False
    assert stt_select.cuda_libraries_present(loader=lambda name: object()) is True


def _stt(tmp_path: Path, factory, downloaded: set[str], **kwargs) -> FasterWhisperSTT:
    candidates = [ModelChoice("large-v3-turbo", "cuda", "float16"), ModelChoice("small.en", "cpu", "int8")]
    return FasterWhisperSTT("x", download_root=tmp_path, model_factory=factory, candidates=candidates,
                            downloaded=lambda m, r: m in downloaded, **kwargs)


def test_gpu_failure_falls_back_to_the_cpu(tmp_path: Path) -> None:
    def factory(name, **kw):
        if kw["device"] == "cuda":
            raise RuntimeError("Library cublas64_12.dll is not found")
        return FakeWhisper([seg("Close phone link.")], **kw)

    stt = _stt(tmp_path, factory, {"large-v3-turbo", "small.en"})
    assert stt.transcribe(speech(), "en").text == "Close phone link."
    assert stt.description == "small.en on CPU"


def test_gpu_failure_during_recognition_falls_back(tmp_path: Path) -> None:
    class BrokenGpu(FakeWhisper):
        def transcribe(self, audio, **kwargs):
            raise RuntimeError("cuDNN failed to initialize")

    def factory(name, **kw):
        return (BrokenGpu if kw["device"] == "cuda" else FakeWhisper)([seg("Open Spotify.")], **kw)

    stt = _stt(tmp_path, factory, {"large-v3-turbo", "small.en"})
    assert stt.transcribe(speech(), "en").text == "Open Spotify."
    assert stt.device == "cpu"


def test_better_model_is_downloaded_in_the_background_and_swapped_in(tmp_path: Path) -> None:
    downloaded = {"small.en"}
    changes: list[str] = []

    def factory(name, **kw):
        if name not in downloaded:
            raise OSError("snapshot not found")
        return FakeWhisper([seg(f"from {name}")], **kw)

    import threading

    go = threading.Event()

    def downloader(model, root):
        go.wait(5)
        downloaded.add(model)

    stt = _stt(tmp_path, factory, downloaded, downloader=downloader)
    stt.on_model_change = changes.append
    assert stt.transcribe(speech(), "en").text == "from small.en"  # usable right away
    go.set()
    stt._upgrading.join(5)
    assert stt.transcribe(speech(), "en").text == "from large-v3-turbo"
    assert changes == ["small.en on CPU", "large-v3-turbo on GPU"]


def test_nothing_downloaded_offline_is_a_friendly_error(tmp_path: Path) -> None:
    stt = _stt(tmp_path, lambda n, **kw: FakeWhisper([]), set(), offline=True)
    with pytest.raises(STTError, match="--setup-voice"):
        stt.load()


def test_quiet_speech_is_amplified() -> None:
    quiet = (0.1 * np.sin(np.arange(16000) / 5)).astype(np.float32)
    louder = normalize_level(quiet)
    assert 0.5 < np.max(np.abs(louder)) <= 0.61
    loud = (0.8 * np.sin(np.arange(160) / 5)).astype(np.float32)
    assert normalize_level(loud) is loud and normalize_level(np.zeros(10, np.float32)).max() == 0


# ------------------------------------------------------------------ neural VAD


@pytest.mark.skipif(silero_model_path() is None, reason="faster-whisper's Silero model is not installed")
def test_silero_vad_ignores_noise_and_tones() -> None:
    vad = create_vad("auto")
    assert isinstance(vad, SileroVAD)
    rng = np.random.default_rng(1)
    noise = (rng.standard_normal(1280) * 0.02).astype(np.float32)
    tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(1280) / 16000)).astype(np.float32)
    assert not any(vad.is_speech(noise) for _ in range(20))
    assert not any(vad.is_speech(tone) for _ in range(20))
    vad.reset()
    assert vad.last_probability < 0.5


def test_energy_vad_on_request() -> None:
    assert isinstance(create_vad("energy"), EnergyVAD)


class FakeSession:
    """Silero stand-in: 'speech' = loud chunk."""

    def run(self, _, feeds):
        chunk = feeds["input"][0][64:]
        p = 0.9 if np.abs(chunk).mean() > 0.1 else 0.1
        return np.array([[p]], np.float32), feeds["h"], feeds["c"]


def test_silero_streaming_with_hysteresis() -> None:
    vad = SileroVAD(session=FakeSession())
    loud = np.full(1280, 0.3, np.float32)
    quiet = np.full(1280, 0.0005, np.float32)
    assert vad.is_speech(loud) and not vad.is_speech(quiet)
    vad.reset()
    assert vad._pending.size == 0 and not vad.is_speech(np.zeros(100, np.float32))


# ------------------------------------------------------------------ misheard names


@pytest.mark.parametrize(("heard", "expected"), [
    ("bone link", "Phone Link"), ("Bon Linked", "Phone Link"), ("spot if I", "Spotify"),
    ("what's up", "WhatsApp"), ("discard", "Discord"), ("note pad", "Notepad"), ("file explore", "File Explorer"),
])
def test_sound_alike_names(heard: str, expected: str) -> None:
    names = ["Phone Link", "Spotify", "WhatsApp", "Discord", "Notepad", "File Explorer", "Google Chrome",
             "Visual Studio Code", "Microsoft Edge", "Calculator", "Steam", "Teams"]
    assert best_sounding(heard, names) == expected


def test_no_guess_for_unrelated_or_ambiguous_names() -> None:
    assert best_sounding("banana bread", ["Phone Link", "Spotify", "Chrome"]) is None
    assert best_sounding("team", ["Teams", "Steam"]) is None  # sounds like both
    assert similarity("", "Chrome") == 0.0


def test_start_menu_match_uses_sound_as_the_last_resort() -> None:
    entries = [("phone link", "Phone Link"), ("photos", "Photos"), ("paint", "Paint")]
    assert best_match(entries, "bone link") == ("phone link", "Phone Link")
    assert best_match(entries, "photos") == ("photos", "Photos")


def test_running_app_names_from_window_titles() -> None:
    windows = [WindowInfo(1, 1, "main.py - Jarvis - Visual Studio Code", "Code.exe"),
               WindowInfo(2, 2, "Phone Link", "PhoneExperienceHost.exe"),
               WindowInfo(3, 3, "hidden", "x.exe", cloaked=True)]
    names = running_app_names(windows)
    assert "Visual Studio Code" in names and "Phone Link" in names and "code" in names
    assert "hidden" not in names


def test_close_corrects_a_misheard_open_app(monkeypatch) -> None:
    from app.tools import applications, winapi

    phone = WindowInfo(7, 7, "Phone Link", "PhoneExperienceHost.exe")
    closed: list[int] = []
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "top_level_windows", lambda: [phone])
    monkeypatch.setattr(winapi, "close_window", closed.append)
    monkeypatch.setattr(winapi, "window_exists", lambda hwnd: False)
    result = applications.close_application(AppNameArgs(name="bone link"), ToolContext(Settings()))
    assert result.ok and result.message == "Closed Phone Link." and closed == [7]


# ------------------------------------------------------------------ direct commands


def known(name: str) -> str | None:
    return name.title() if name.lower() in {"chrome", "spotify", "notepad", "whatsapp", "phone link"} else None


@pytest.mark.parametrize(("text", "tool", "args"), [
    ("What time is it?", "get_datetime", {}),
    ("what's the date today", "get_datetime", {}),
    ("how much battery do I have left", "system_status", {"aspect": "battery"}),
    ("minimize chrome", "window_control", {"action": "minimize", "name": "chrome"}),
    ("switch to WhatsApp", "window_control", {"action": "focus", "name": "WhatsApp"}),
    ("show me the desktop", "window_control", {"action": "minimize_all"}),
])
def test_instant_and_window_commands(text: str, tool: str, args: dict) -> None:
    call = match_shortcut(text, known)
    assert call is not None and (call.name, dict(call.arguments)) == (tool, args)


def test_unknown_window_target_goes_to_the_model() -> None:
    assert match_shortcut("minimize the window", known) is None
    assert match_shortcut("switch to dark mode", known) is None


def test_split_commands() -> None:
    assert split_commands("open Chrome and Spotify") == ["open Chrome", "open Spotify"]
    assert split_commands("close notepad, then lock my PC") == ["close notepad", "lock my PC"]
    assert split_commands("open chrome") == []
    assert split_commands("open Chrome and then close Notepad and Spotify") == ["open Chrome", "close Notepad", "close Spotify"]


def test_references() -> None:
    assert resolve_reference("close it", "Phone Link") == "close Phone Link"
    assert resolve_reference("Open that again.", "Spotify") == "Open Spotify."
    assert resolve_reference("reopen it", "Spotify") == "open Spotify"
    assert resolve_reference("close it", None) == "close it"
    assert resolve_reference("close the door", "Spotify") == "close the door"


def _conversation(replies=()):
    calls: list[tuple[str, str]] = []

    def opener(args, ctx):
        calls.append(("open", args.name))
        return ToolResult.success(f"Opened {args.name.title()}.", app=args.name.title())

    def closer(args, ctx):
        calls.append(("close", args.name))
        return ToolResult.success(f"Closed {args.name.title()}.", app=args.name.title())

    registry = ToolRegistry([Tool("open_application", "open", opener, AppNameArgs),
                             Tool("close_application", "close", closer, AppNameArgs)])
    settings = Settings()
    fake = FakeOllama(models=[model_entry("llama3.2:3b")], replies=list(replies))
    ex = ToolExecutor(registry, PermissionEngine(PermissionsConfig()), ToolContext(settings, clock=lambda: NOW),
                      confirmer=ScriptedConfirmer(True))
    conv = ConversationManager(AIManager.from_settings(settings, transport=fake.transport()), settings,
                               clock=lambda: NOW, tools=ex)
    return conv, calls, fake


def _send(conv, *texts):
    async def go():
        try:
            return [await conv.send(t) for t in texts]
        finally:
            await conv.ai.aclose()
    return asyncio.run(go())


def test_chained_commands_run_directly_and_it_means_the_last_app() -> None:
    conv, calls, fake = _conversation()
    first, second = _send(conv, "open Chrome and Notepad", "close it")
    assert calls == [("open", "Chrome"), ("open", "Notepad"), ("close", "Notepad")]
    assert first.text == "Opened Chrome. Opened Notepad." and first.model == "direct"
    assert second.text == "Closed Notepad."
    assert not [r for r in fake.requests if r[1] == "/api/chat"]  # the model was never needed
    assert conv.recent_actions[-1].endswith("Closed Notepad.") and conv.last_app == "Notepad"
    conv.reset()
    assert conv.last_app is None and not conv.recent_actions


def test_partly_complex_chains_go_to_the_model() -> None:
    conv, calls, fake = _conversation([say("Opened it and searched.")])
    (reply,) = _send(conv, "open Chrome and search for cats")
    assert reply.text == "Opened it and searched." and calls == []


def test_recent_actions_in_the_prompt() -> None:
    prompt = build_system_prompt(assistant_name="Jarvis", now=NOW, recent_actions=["21:43 Opened Phone Link."])
    assert "21:43 Opened Phone Link." in prompt and "\"it\"" in prompt
    assert "What you did earlier" not in build_system_prompt(assistant_name="Jarvis", now=NOW)


# ------------------------------------------------------------------ faster first answer


def test_warm_up_loads_the_model() -> None:
    fake = FakeOllama(models=[model_entry("llama3.2:3b")])
    ai = AIManager.from_settings(Settings(), transport=fake.transport())

    async def go():
        try:
            return await ai.warm_up()
        finally:
            await ai.aclose()

    assert asyncio.run(go()) == "llama3.2:3b"
    body = next(b for _, p, b in fake.requests if p == "/api/generate")
    assert body["model"] == "llama3.2:3b" and body["prompt"] == "" and body["keep_alive"] == "30m"


def test_warm_up_never_raises() -> None:
    fake = FakeOllama(models=[])
    ai = AIManager.from_settings(Settings(), transport=fake.transport())

    async def go():
        try:
            return await ai.warm_up()
        finally:
            await ai.aclose()

    assert asyncio.run(go()) is None


def test_settings_accept_the_new_values() -> None:
    assert Settings(vad_engine="SILERO").vad_engine == "silero"
    with pytest.raises(Exception):
        Settings(vad_engine="magic")
    assert SimpleNamespace  # (keeps the import list honest)
