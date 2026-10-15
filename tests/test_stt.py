from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.voice.speech_to_text import FasterWhisperSTT, STTError, is_hallucination
from app.voice.wake_word import WakeWordUnavailableError, create_wake_word_detector, strip_wake_phrase

def speech(n: int = 16000):
    """Audible test audio (pure silence is skipped before recognition)."""
    return (0.1 * np.sin(np.arange(n) / 3)).astype(np.float32)



class FakeWhisper:
    def __init__(self, segments, **kwargs) -> None:
        self.kwargs = kwargs
        self.segments = segments
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return iter(self.segments), SimpleNamespace(language=kwargs.get("language") or "en")


def seg(text: str, logprob: float = -0.1, no_speech: float = 0.01):
    return SimpleNamespace(text=text, avg_logprob=logprob, no_speech_prob=no_speech)


def make(tmp_path: Path, segments, **kwargs) -> tuple[FasterWhisperSTT, dict]:
    created: dict = {}

    def factory(name, **kw):
        created.update(kw, name=name)
        created["model"] = FakeWhisper(segments, **kw)
        return created["model"]

    return FasterWhisperSTT("base.en", download_root=tmp_path, model_factory=factory, **kwargs), created


def test_transcribe_text_and_confidence(tmp_path: Path) -> None:
    stt, created = make(tmp_path, [seg(" Open Chrome.", -0.2), seg(" Now.", -0.4)])
    t = stt.transcribe(speech(), "en")
    assert t.text == "Open Chrome. Now." and t.language == "en"
    assert t.confidence == pytest.approx(math.exp((-0.2 * 13 + -0.4 * 5) / 18), rel=1e-3)  # weighted by text length
    assert created["name"] == "base.en" and created["local_files_only"] is False
    call = created["model"].calls[0]
    assert call["vad_filter"] is True and call["condition_on_previous_text"] is False


def test_hotwords_passed_to_model(tmp_path: Path) -> None:
    from app.cli_voice import recognition_hints

    assert recognition_hints("hey jarvis", "Jarvis") == "Hey Jarvis, Jarvis"
    assert recognition_hints("hey nova", "Jarvis", ["WhatsApp", "whatsapp", "Chrome"]) == "Hey Nova, Jarvis, WhatsApp, Chrome"
    assert len(recognition_hints("hey jarvis", "Jarvis", [f"App{i}" for i in range(500)]).split()) <= 70
    stt, created = make(tmp_path, [seg("Hey Jarvis")], hotwords="Hey Jarvis")
    stt.transcribe(speech(), "en")
    assert created["model"].calls[0]["hotwords"] == "Hey Jarvis"


def test_auto_language_and_offline(tmp_path: Path) -> None:
    stt, created = make(tmp_path, [seg("hola")], offline=True)
    stt.transcribe(speech(), "auto")
    assert created["model"].calls[0]["language"] is None and created["local_files_only"] is True


def test_short_audio_skipped_and_hallucination_removed(tmp_path: Path) -> None:
    stt, _ = make(tmp_path, [seg(" Thank you.", -0.5, 0.6)])
    assert stt.transcribe(np.zeros(1000, dtype=np.float32)).is_empty
    assert stt.transcribe(speech()).is_empty
    assert not is_hallucination("Thank you, open Chrome", 0.9)


def test_load_errors_are_friendly(tmp_path: Path) -> None:
    def boom(name, **kw):
        raise RuntimeError("Library cublas64_12.dll is not found")

    with pytest.raises(STTError, match="STT_DEVICE=cpu"):
        FasterWhisperSTT("base.en", download_root=tmp_path, model_factory=boom, device="cuda").load()

    def missing(name, **kw):
        raise OSError("Cannot find an appropriate cached snapshot folder")

    with pytest.raises(STTError, match="--setup-voice"):
        FasterWhisperSTT("base.en", download_root=tmp_path, model_factory=missing, offline=True).load()


@pytest.mark.parametrize(
    ("spoken", "expected", "found"),
    [
        ("Hey Jarvis, open Visual Studio Code.", "open Visual Studio Code.", True),
        ("hey, jarvis. what time is it?", "what time is it?", True),
        ("Hi Jervis open chrome", "open chrome", True),
        ("Jarvis, mute my computer", "mute my computer", True),
        ("As Jarvis opened Google Chrome", "opened Google Chrome", True),
        ("Hello Jarvis, what's my battery?", "what's my battery?", True),
        ("Hey Jarvis.", "", True),
        ("Open Chrome", "Open Chrome", False),
        ("Hey there, open Chrome", "Hey there, open Chrome", False),
    ],
)
def test_strip_wake_phrase(spoken: str, expected: str, found: bool) -> None:
    assert strip_wake_phrase(spoken, "hey jarvis") == (expected, found)


def test_wake_word_detector_needs_its_models(tmp_path) -> None:
    with pytest.raises(WakeWordUnavailableError, match="setup-voice"):
        create_wake_word_detector("hey jarvis", model_dir=tmp_path)
    with pytest.raises(WakeWordUnavailableError, match="not available"):
        create_wake_word_detector("hey jarvis", "snowboy", model_dir=tmp_path)
    with pytest.raises(WakeWordUnavailableError, match="no ready-made"):
        create_wake_word_detector("hey friday", model_dir=tmp_path)
