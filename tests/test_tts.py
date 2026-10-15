from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.config.settings import Settings
from app.tts.base import AudioClip, TTSEngine, TTSError
from app.tts.manager import TTSManager, create_engine
from app.tts.piper import PiperTTS, download_voice, is_voice_installed, parse_voice_id
from app.tts.player import SoundDevicePlayer
from app.tts.text import SentenceSplitter, clean_for_speech
from tests.fake_audio import FakeSoundDevice


class FakeEngine(TTSEngine):
    name = "fake"

    def __init__(self, delay: float = 0.0) -> None:
        self.spoken: list[str] = []
        self.delay = delay

    def load(self) -> None:
        pass

    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        time.sleep(self.delay)
        self.spoken.append(text)
        return AudioClip(np.zeros(2205, dtype=np.float32), 22050)


class FakePlayer:
    def __init__(self, seconds: float = 0.0) -> None:
        self.seconds = seconds
        self.played = 0
        self.aborted = 0

    def play(self, clip, *, volume, abort: threading.Event) -> bool:
        end = time.monotonic() + self.seconds
        while time.monotonic() < end:
            if abort.is_set():
                self.aborted += 1
                return False
            time.sleep(0.005)
        self.played += 1
        return True


def manager(engine=None, player=None, **settings) -> TTSManager:
    return TTSManager(Settings(**settings), Path("."), engine=engine or FakeEngine(), player=player or FakePlayer())


def test_queue_speaks_in_order() -> None:
    engine = FakeEngine()
    states: list[bool] = []
    tts = manager(engine)
    tts.on_speaking_change = states.append
    try:
        for text in ("One.", "Two **bold**.", "Three."):
            tts.speak(text)
        assert tts.wait_until_idle(5)
        assert engine.spoken == ["One.", "Two bold.", "Three."]
        assert states == [True, False] or states[0] is True and states[-1] is False
    finally:
        tts.close()


def test_stop_interrupts_and_clears_queue() -> None:
    engine, player = FakeEngine(), FakePlayer(seconds=2.0)
    tts = manager(engine, player)
    try:
        for i in range(5):
            tts.speak(f"Sentence {i}.")
        time.sleep(0.2)
        started = time.monotonic()
        tts.stop()
        assert tts.wait_until_idle(2) and time.monotonic() - started < 1.0
        assert player.aborted == 1 and len(engine.spoken) <= 3  # the next sentences were prepared ahead, not played
        tts.speak("After stop.")  # queue works again afterwards
        assert tts.wait_until_idle(5) and engine.spoken[-1] == "After stop."
    finally:
        tts.close()


def test_mute_and_disabled() -> None:
    engine = FakeEngine()
    tts = manager(engine)
    try:
        tts.set_muted(True)
        assert tts.speak("hello").is_set()
        tts.set_muted(False)
        tts.speak("hello")
        assert tts.wait_until_idle(5) and engine.spoken == ["hello"]
    finally:
        tts.close()
    disabled = manager(tts_enabled=False)
    assert disabled.speak("x").is_set()
    disabled.close()


def test_engine_failure_is_reported_not_raised() -> None:
    class Broken(FakeEngine):
        def synthesize(self, text, speed=1.0):
            raise TTSError("synthesis failed")

    tts = manager(Broken())
    try:
        tts.speak("hi")
        assert tts.wait_until_idle(5) and tts.problem == "synthesis failed"
    finally:
        tts.close()


def test_auto_engine_selection(tmp_path: Path) -> None:
    engine, problem = create_engine(Settings(tts_engine="piper"), tmp_path)
    assert engine is None and "--setup-voice" in problem


def test_text_cleaning_and_splitting() -> None:
    assert clean_for_speech("**Done!** See https://x.com 🙂\n- item") == "Done! See the link item"
    splitter = SentenceSplitter()
    out = []
    for piece in ["Your CPU is ", "at 37%. RAM us", "age is 8.5 GB, e.g. normal. Anything", " else?"]:
        out += splitter.feed(piece)
    assert out == ["Your CPU is at 37%.", "RAM usage is 8.5 GB, e.g. normal."]
    assert splitter.flush() == "Anything else?"


def test_player_resamples_for_unsupported_rate() -> None:
    sd = FakeSoundDevice(supported_rates=(48000,))
    ok = SoundDevicePlayer(sd=sd).play(AudioClip(np.ones(22050, dtype=np.float32) * 2, 22050), volume=0.5, abort=threading.Event())
    played = np.concatenate(sd.played)
    assert ok and sd.output_rates == [48000] and abs(played.size - 48000) < 10 and played.max() <= 1.0


def test_piper_voice_download_and_load(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/")
        if request.url.path.endswith(".json"):
            return httpx.Response(200, content=json.dumps({"audio": {"sample_rate": 22050}}).encode())
        return httpx.Response(200, content=b"\0" * 1000, headers={"content-length": "1000"})

    progress: list[int] = []
    download_voice("en_US-lessac-medium", tmp_path, progress=lambda n, d, t: progress.append(d),
                   transport=httpx.MockTransport(handler))
    assert is_voice_installed(tmp_path, "en_US-lessac-medium") and progress[-1] == 1000
    assert not list(tmp_path.glob("*.part"))

    chunk = SimpleNamespace(audio_float_array=np.full(100, 0.1, dtype=np.float32), sample_rate=22050)
    voice = SimpleNamespace(synthesize=lambda text, syn_config=None: iter([chunk, chunk]))
    loaded: dict = {}
    engine = PiperTTS("en_US-lessac-medium", tmp_path, loader=lambda path, **kw: loaded.update(kw, path=path) or voice)
    clip = engine.synthesize("hello")
    assert clip.samples.size == 200 and clip.sample_rate == 22050
    assert loaded["path"].endswith("en_US-lessac-medium.onnx") and engine.voices() == ["en_US-lessac-medium"]


def test_piper_errors(tmp_path: Path) -> None:
    with pytest.raises(TTSError, match="not a valid"):
        parse_voice_id("lessac")
    with pytest.raises(TTSError, match="--setup-voice"):
        PiperTTS("en_US-lessac-medium", tmp_path).load()
    with pytest.raises(TTSError, match="does not exist"):
        download_voice("en_US-nobody-low", tmp_path, transport=httpx.MockTransport(lambda r: httpx.Response(404)))
