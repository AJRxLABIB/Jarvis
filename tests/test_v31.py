"""3.1: short-command hearing, benchmark recorder, wake engine for a bare "Jarvis", fact checking,
the start-up intro, full screen, HUD motion."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app import benchmark
from app.agent.shortcuts import match_shortcut
from app.config.settings import Settings
from app.voice.speech_to_text import FILLERS, FasterWhisperSTT


def seg(text: str, logprob: float = -0.2, no_speech: float = 0.05):
    return SimpleNamespace(text=text, avg_logprob=logprob, no_speech_prob=no_speech)


class ClippedWhisper:
    """Says "Thank you." with the voice filter on; hears the real words without it."""

    def __init__(self, real: str, logprob: float = -0.9) -> None:
        self.real, self.logprob = real, logprob
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        text = "Thank you." if kwargs.get("vad_filter", True) else self.real
        return iter([seg(text, self.logprob)]), SimpleNamespace(language=kwargs.get("language") or "en")


def quiet_speech(seconds: float = 0.7) -> np.ndarray:
    return (0.02 * np.sin(np.arange(int(16000 * seconds)) / 3)).astype(np.float32)


def make(tmp_path: Path, model) -> FasterWhisperSTT:
    return FasterWhisperSTT("large-v3-turbo", download_root=tmp_path, model_factory=lambda name, **kw: model,
                            background_upgrade=False, second_pass=False)


# ------------------------------------------------------------------ "Volume up" heard as "Thank you."

def test_filler_is_decoded_again_without_the_voice_filter(tmp_path: Path) -> None:
    model = ClippedWhisper("Volume up.")
    result = make(tmp_path, model).transcribe(quiet_speech(), "en")
    assert result.text == "Volume up."
    assert [c.get("vad_filter") for c in model.calls] == [True, False]


def test_a_filler_that_stays_a_filler_is_not_heard_when_unsure(tmp_path: Path) -> None:
    model = ClippedWhisper("Thank you.")
    assert make(tmp_path, model).transcribe(quiet_speech(), "en").text == ""


def test_a_clear_thank_you_is_kept(tmp_path: Path) -> None:
    model = ClippedWhisper("Thank you.", logprob=-0.05)  # sure of it: you really said thanks
    assert make(tmp_path, model).transcribe(quiet_speech(), "en").text == "Thank you."


def test_long_utterances_are_not_second_guessed(tmp_path: Path) -> None:
    model = ClippedWhisper("Volume up.")
    make(tmp_path, model).transcribe(quiet_speech(5.0), "en")
    assert len(model.calls) == 1
    assert "thank you" in FILLERS and "open chrome" not in FILLERS


# ------------------------------------------------------------------ benchmark recorder

def test_trim_silence_keeps_the_speech() -> None:
    rate = 16000
    audio = np.concatenate([np.zeros(rate), 0.3 * np.sin(np.arange(rate // 2) / 3), np.zeros(rate * 2)]).astype(np.float32)
    trimmed = benchmark.trim_silence(audio)
    assert 0.5 * rate <= trimmed.size <= 1.3 * rate
    assert float(np.max(np.abs(trimmed))) > 0.2


def test_level_advice() -> None:
    assert "very quiet" in benchmark.level_advice(-40)
    assert "speak up" in benchmark.level_advice(-30)
    assert benchmark.level_advice(-18) == ""


def test_bangla_prompts_are_readable_in_any_console() -> None:
    bangla = [s for s, _, _ in benchmark.PROMPTS if any("ঀ" <= c <= "৿" for c in s)]
    assert bangla and all(s in benchmark.ROMANIZED for s in bangla)
    assert benchmark.spoken_hint("ক্রোম খোলো") == "ক্রোম খোলো   (chrome kholo - say it in Bangla)"
    assert benchmark.spoken_hint("Volume up") == "Volume up"


# ------------------------------------------------------------------ wake engine

def test_bare_jarvis_uses_the_neural_model_with_a_check() -> None:
    from app.voice.hands_free import BARE_NAME_NEAR_MISS, NEAR_MISS_SCORE, resolve_engine
    from app.voice.wake_word import is_bare_name, model_files_for, model_phrase

    assert model_phrase("jarvis") == "hey jarvis" and model_phrase("Hey  Jarvis") == "hey jarvis"
    assert model_phrase("computer") is None
    assert is_bare_name("jarvis") and not is_bare_name("hey jarvis")
    assert resolve_engine(Settings(wake_word="jarvis")) == "openwakeword"
    assert resolve_engine(Settings(wake_word="computer")) == "whisper"
    assert any(f.name == "hey_jarvis_v0.1.onnx" for f in model_files_for("jarvis"))
    assert BARE_NAME_NEAR_MISS < NEAR_MISS_SCORE


# ------------------------------------------------------------------ answers

def test_prompt_asks_to_check_facts_it_is_unsure_of() -> None:
    from datetime import datetime

    from app.ai.prompts import build_system_prompt

    tools = [SimpleNamespace(name="web_search", description="", parameters={})]
    prompt = build_system_prompt(assistant_name="Jarvis", now=datetime(2026, 9, 24), tools=tools)
    assert "not completely sure of: look them up with web_search first" in prompt


def test_model_comparison_loads_each_model_first() -> None:
    from app import eval_bangla, model_compare

    loaded = []

    async def prepare(model):
        loaded.append(model)

    def ask_for(model):
        async def ask(question):
            assert loaded and loaded[-1] == model  # loaded before its first question
            return "ঢাকা"
        return ask

    asyncio.run(model_compare.compare(["a", "b"], ask_for, lambda _: None, eval_bangla.QUESTIONS[:1], prepare))
    assert loaded == ["a", "b"]


# ------------------------------------------------------------------ full screen by voice

@pytest.mark.parametrize("said, action", [
    ("make yourself full screen", "full_screen"), ("go full screen yourself", "full_screen"),
    ("নিজেকে ফুল স্ক্রিন করো", "full_screen"), ("exit your full screen", "exit_full_screen"),
    ("make yourself smaller", "exit_full_screen"),
])
def test_full_screen_commands(said, action) -> None:
    call = match_shortcut(said, lambda n: None)
    assert call is not None and call.name == "jarvis_control" and call.arguments["action"] == action


def test_full_screen_alone_is_still_for_the_app_in_front() -> None:
    call = match_shortcut("full screen", lambda n: None)
    assert call is not None and call.name == "app_command"


def test_settings_for_the_start() -> None:
    s = Settings()
    assert s.intro_animation and s.start_fullscreen


# ------------------------------------------------------------------ intro and HUD (needs Qt)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except (ImportError, OSError) as exc:  # pragma: no cover
        pytest.skip(f"Qt is not available here: {exc}")
    return QApplication.instance() or QApplication([])


def test_intro_draws_every_moment_and_shows_real_states(qapp) -> None:
    from PySide6.QtGui import QImage, QPainter

    from app.ui.intro import DURATION, FINALE, IntroScreen

    asked = []

    def state():
        asked.append(1)
        return ("LOADING", "wait")

    intro = IntroScreen("J.A.R.V.I.S.", "test", [("Speech recognition", state)])
    intro.resize(800, 450)
    for t in (0.0, 0.5, 1.2, 2.0, 2.8, FINALE, FINALE + 0.3, DURATION - 0.1):
        image = QImage(800, 450, QImage.Format.Format_RGB32)
        intro.paint(QPainter(image), t)
    assert asked  # the boot log asked the real state, it didn't make one up


def test_intro_skip_jumps_to_the_finale(qapp) -> None:
    from app.ui.intro import FINALE, IntroScreen

    now = [100.0]
    intro = IntroScreen("J", "", [], clock=lambda: now[0])
    intro._t0 = 100.0
    now[0] = 100.5
    intro.skip()
    assert intro.elapsed() >= FINALE
    fired = []
    intro.finished.connect(lambda: fired.append(1))
    intro._frame()
    assert fired == [1]


def test_panels_replay_their_entrance_and_follow_the_state(qapp) -> None:
    import time

    from app.ui.dashboard import DashPanel

    panel = DashPanel("Test")
    panel.show()
    panel._born = 0.0
    assert panel.boot_progress() == 1.0
    DashPanel.replay_boot()
    assert panel.boot_progress() < 1.0
    DashPanel.set_activity("THINKING")
    try:
        assert panel._tick(time.monotonic())  # keeps moving while Jarvis thinks
    finally:
        DashPanel.set_activity("IDLE")
    panel.close()


def test_tray_brings_back_full_screen() -> None:
    from app.ui.tray import TrayController

    seen = []
    fake = SimpleNamespace(window=SimpleNamespace(bring_back=lambda: seen.append("back")))
    TrayController.show_window(fake)
    assert seen == ["back"]
