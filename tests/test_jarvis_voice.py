"""The JARVIS voice profile (3.21): British neural voice, calmer/deeper, subtle AI finish; the only voice offered."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from app.config.settings import Settings
from app.tts.base import AudioClip, TTSEngine
from app.tts.jarvis_voice import PEAK, JarvisVoice, jarvis_effect, low_cut
from app.tts.neural import NeuralTTS, prosody


def tone(seconds: float = 0.5, rate: int = 24000) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_effect_adds_a_short_room_and_evens_the_level() -> None:
    dry = tone()
    wet = jarvis_effect(dry, 24000)
    assert wet.dtype == np.float32 and dry.size < wet.size < dry.size + 24000 * 0.1  # a short tail, not an echo
    assert abs(float(np.max(np.abs(wet))) - PEAK) < 1e-3
    correlation = np.corrcoef(dry, wet[:dry.size])[0, 1]
    assert correlation > 0.8  # still clearly the same words
    assert jarvis_effect(np.zeros(0, np.float32), 24000).size == 0


def test_low_cut_removes_rumble() -> None:
    rate = 24000
    rumble = np.full(rate, 0.5, np.float32)  # DC / very low frequency
    assert abs(float(np.mean(low_cut(rumble, rate)))) < 0.01


def test_jarvis_is_calmer_and_deeper_in_english_only() -> None:
    assert prosody("Good evening, sir.", 1.0, -6, -3) == ("-6%", "-3Hz")
    calls = []
    engine = NeuralTTS("en-GB-RyanNeural", "bn-BD-PradeepNeural", rate_offset=-6, pitch_offset=-3,
                       synthesize_mp3=lambda text, voice, rate, pitch: calls.append((voice, rate, pitch)) or b"x",
                       decoder=lambda data: np.ones(100, np.float32) * 0.1)
    engine.synthesize("Systems online.")
    engine.synthesize("আমি প্রস্তুত।")
    assert calls[0] == ("en-GB-RyanNeural", "-6%", "-3Hz") and calls[1][0] == "bn-BD-PradeepNeural"
    assert calls[1][1:] == ("+0%", "+0Hz")


class Plain(TTSEngine):
    name = "plain"

    def load(self) -> None: ...

    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        return AudioClip(tone(0.2), 24000)


def test_wrapper_applies_the_finish() -> None:
    voice = JarvisVoice(Plain())
    clip = voice.synthesize("hello")
    assert clip.samples.size > tone(0.2).size and voice.voices() == ["JARVIS"] and "JARVIS voice" in voice.description


def test_jarvis_is_the_default_and_the_only_choice_offered(monkeypatch, tmp_path: Path) -> None:
    assert Settings().tts_engine == "jarvis"
    from app.tts import manager

    monkeypatch.setattr(NeuralTTS, "load", lambda self: None)
    engine, problem = manager.create_engine(Settings(tts_engine="jarvis"), tmp_path / "piper")
    assert problem is None and isinstance(engine, JarvisVoice) and isinstance(engine.inner, NeuralTTS)
    assert engine.inner.english_voice == "en-GB-RyanNeural" and engine.inner.pitch_offset < 0
    source = (Path(__file__).parents[1] / "app" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    assert 'FieldSpec("tts_engine", "Voice", "choice", options=("jarvis",)' in source
    assert "tts_kokoro_voice" not in source and "tts_sapi_voice" not in source
