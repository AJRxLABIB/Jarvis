"""Hands-free listening: wake phrase -> command -> result, with a fake microphone and detector."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from app.config.settings import Settings
from app.voice import wake_word
from app.voice.audio import AudioError
from app.voice.hands_free import (
    FRAME_SAMPLES, HandsFreeListener, UtteranceCollector, prepare_detector, resolve_engine, wake_chime,
)
from app.voice.microphone import Microphone
from app.voice.vad import EnergyVAD
from app.voice.wake_word import (
    ModelFile, OnnxWakeWordDetector, WakeWordDetector, WakeWordUnavailableError, download_models, missing_models,
)
from tests.fake_audio import FakeSoundDevice, noise, tone
from tests.test_voice_pipeline import FakeSTT

WAKE = 0.9  # the fake detector "hears the wake phrase" in any frame this loud


class LoudDetector(WakeWordDetector):
    def __init__(self) -> None:
        self.resets = 0
        self.last_score = 0.0

    near_miss_level = None  # frames this loud score like "Jarvis, are you there?" (almost the wake phrase)

    def process(self, frame: np.ndarray) -> bool:
        peak = float(np.max(np.abs(frame)))
        near = self.near_miss_level is not None and abs(peak - self.near_miss_level) < 0.02
        self.last_score = 0.99 if peak >= WAKE - 0.05 else (0.35 if near else 0.05)  # like the real model
        return self.last_score >= 0.5

    def reset(self) -> None:
        self.resets += 1


def wake_then(command_seconds: float = 0.8, gap: float = 0.1, tail: float = 3.0) -> np.ndarray:
    parts = [noise(0.96), tone(0.16, WAKE), noise(gap)]  # frame-aligned: detection on the 1st loud frame
    if command_seconds:
        parts.append(tone(command_seconds, 0.3))
    parts.append(noise(tail))
    return np.concatenate(parts)


class Harness:
    def __init__(self, audio: np.ndarray, *, text: str = "hey jarvis open chrome", confidence: float = 0.9,
                 detector: WakeWordDetector | None = None, whisper: bool = False, pause=lambda: False,
                 realtime: float = 0.0, **settings) -> None:
        self.sd = FakeSoundDevice(audio, realtime=realtime)  # realtime: pace the audio like a real microphone
        self.results, self.states, self.notices, self.wakes = [], [], [], []
        self.settings = Settings(hands_free=True, silence_timeout=0.5, **settings)
        self.stt = FakeSTT(text, confidence)
        self.detector = detector or LoudDetector()
        self.listener = HandsFreeListener(
            self.settings, Microphone(sd=self.sd), self.stt,
            detector_factory=None if whisper else (lambda: self.detector),
            on_result=self.results.append, on_state=self.states.append, on_notice=self.notices.append,
            on_wake=lambda: self.wakes.append(1), should_pause=pause)
        self.listener._mic_watch.seconds = 1e12  # the fake stream delivers silence faster than real time

    def run_until(self, condition, timeout: float = 5.0) -> bool:
        self.listener.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            time.sleep(0.01)
        return condition()

    def stop(self) -> None:
        self.listener.stop()


# ----------------------------------------------------------------- collector


def test_collector_ends_after_a_pause() -> None:
    vad = EnergyVAD()
    collector = UtteranceCollector(vad, start_timeout=5, silence_timeout=0.4, max_seconds=10)
    frames = np.concatenate([noise(0.8), tone(0.64, 0.3), noise(1.0)])
    result = None
    for i in range(0, frames.size - FRAME_SAMPLES, FRAME_SAMPLES):
        frame = frames[i:i + FRAME_SAMPLES]
        result = collector.feed(frame, vad.is_speech(frame)) or result
        if result:
            break
    assert result is not None and result.speech and 0.5 < result.audio.size / 16000 < 1.3


def test_collector_times_out_without_speech() -> None:
    vad = EnergyVAD()
    collector = UtteranceCollector(vad, start_timeout=0.4, silence_timeout=0.4, max_seconds=10)
    results = [collector.feed(np.zeros(FRAME_SAMPLES, np.float32), False) for _ in range(6)]
    done = [r for r in results if r is not None]
    assert len(done) == 1 and not done[0].speech


def test_collector_drops_short_bursts_and_keeps_waiting() -> None:
    """The tail of the wake phrase, a click or a cough is not a command."""
    vad = EnergyVAD()
    collector = UtteranceCollector(vad, start_timeout=6, silence_timeout=0.4, max_seconds=10)
    frames = np.concatenate([noise(0.8), tone(0.08, 0.3), noise(0.8), tone(0.8, 0.3), noise(1.0)])
    results = []
    for i in range(0, frames.size - FRAME_SAMPLES, FRAME_SAMPLES):
        frame = frames[i:i + FRAME_SAMPLES]
        result = collector.feed(frame, vad.is_speech(frame))
        if result is not None:
            results.append(result)
    assert len(results) == 1 and results[0].speech and results[0].audio.size / 16000 >= 0.8


# ------------------------------------------------------------------ listener


def test_wake_phrase_then_command() -> None:
    h = Harness(wake_then())
    try:
        assert h.run_until(lambda: h.results)
        result = h.results[0]
        assert result.ok and result.text == "open chrome" and h.wakes == [1]
        assert h.states[:3] == ["starting", "waiting", "command"] and h.listener.state == "busy"
    finally:
        h.stop()
    assert h.states[-1] == "stopped" and h.sd.open_streams == 0  # the microphone is always closed


def test_nothing_happens_without_the_wake_phrase() -> None:
    h = Harness(np.concatenate([noise(1.0), tone(1.0, 0.3), noise(2.0)]), text="We should open the window.")
    try:
        assert not h.run_until(lambda: h.results, timeout=1.5)
        assert h.wakes == [] and h.listener.state == "waiting"
    finally:
        h.stop()


def test_wake_phrase_alone_is_answered() -> None:
    """ "Jarvis." and nothing else: "Yes, sir! How can I help you?" (then the conversation continues)."""
    h = Harness(wake_then(command_seconds=0, tail=8.0), user_address="sir")
    try:
        assert h.run_until(lambda: h.results, timeout=10)
        assert h.results[0].status == "wake_only" and h.results[0].message == "Yes, sir! How can I help you?"
    finally:
        h.stop()


def test_follow_up_needs_no_wake_phrase() -> None:
    audio = np.concatenate([wake_then(tail=1.5), tone(0.6, 0.3), noise(3.0)])
    h = Harness(audio, text="what time is it")
    try:
        assert h.run_until(lambda: h.results)
        h.listener.resume(follow_up=True)
        assert h.run_until(lambda: len(h.results) == 2)
        assert h.results[1].text == "what time is it" and "follow_up" in h.states and h.wakes == [1]
    finally:
        h.stop()


def test_resume_without_follow_up_waits_for_the_wake_phrase() -> None:
    h = Harness(np.concatenate([wake_then(tail=0.8), tone(0.6, 0.3), noise(3.0)]))
    try:
        assert h.run_until(lambda: h.results)
        h.stt.text = "and then we went home"  # later speech without the name
        h.listener.resume(follow_up=False)
        assert not h.run_until(lambda: len(h.results) > 1, timeout=1.0)
        assert h.listener.state == "waiting"
    finally:
        h.stop()


def test_paused_while_the_assistant_speaks() -> None:
    speaking = threading.Event()
    speaking.set()
    h = Harness(wake_then(), pause=speaking.is_set)
    try:
        assert not h.run_until(lambda: h.wakes, timeout=1.0)  # its own voice never wakes it
        assert h.detector.resets > 0
    finally:
        h.stop()


def test_talk_button_skips_the_wake_phrase() -> None:
    h = Harness(np.concatenate([noise(1.0), noise(0.4), tone(0.8, 0.3), noise(3.0)]), text="open notepad")
    try:
        assert h.run_until(lambda: h.listener.state == "waiting")
        h.listener.trigger()
        assert h.run_until(lambda: h.results)
        assert h.results[0].text == "open notepad"
    finally:
        h.stop()


def test_cancel_drops_the_command() -> None:
    h = Harness(noise(12.0))
    try:
        assert h.run_until(lambda: h.listener.state == "waiting")
        h.listener.trigger()  # Talk: a command without the wake word (and without a greeting timer)
        assert h.run_until(lambda: h.listener.state == "command")
        h.listener.cancel()
        assert h.run_until(lambda: h.listener.state == "waiting")
        assert h.results == []
    finally:
        h.stop()


def test_low_confidence_is_reported_not_run() -> None:
    h = Harness(wake_then(), confidence=0.1)
    try:
        assert h.run_until(lambda: h.results)
        assert h.results[0].status == "low_confidence" and not h.results[0].ok
    finally:
        h.stop()


def test_whisper_engine_ignores_speech_without_the_wake_phrase() -> None:
    h = Harness(np.concatenate([noise(1.0), tone(0.8, 0.3), noise(3.0)]), text="we should open the window",
                whisper=True)
    try:
        assert not h.run_until(lambda: h.results, timeout=1.5)
        assert h.wakes == []
    finally:
        h.stop()


def test_whisper_engine_with_wake_phrase() -> None:
    h = Harness(np.concatenate([noise(1.0), tone(0.8, 0.3), noise(3.0)]), text="Hey Jarvis, open Chrome.",
                whisper=True)
    try:
        assert h.run_until(lambda: h.results)
        assert h.results[0].text == "open Chrome." and h.wakes == [1]
    finally:
        h.stop()


def test_microphone_errors_are_retried_and_it_recovers(monkeypatch) -> None:
    """At sign-in the microphone is often not ready yet: keep trying, say so once, and carry on when it's back."""
    h = Harness(wake_then())
    real_open = h.listener.microphone.open_stream
    calls = []

    def flaky(frame_samples):
        calls.append(1)
        if len(calls) <= 5:
            raise AudioError("Could not open the microphone: device busy")
        return real_open(frame_samples)

    monkeypatch.setattr(h.listener.microphone, "open_stream", flaky)
    monkeypatch.setattr(h.listener._stop, "wait", lambda seconds: False)  # no real back-off in tests
    try:
        assert h.run_until(lambda: h.results)  # it works once the microphone is there
        assert len(calls) == 6 and h.results[0].text == "open chrome"
        waiting = [n for n in h.notices if "waiting for the microphone" in n]
        assert len(waiting) == 1 and "working again" in h.notices[-1]
    finally:
        h.stop()


def test_retry_delays_grow_to_thirty_seconds() -> None:
    h = Harness(noise(1.0))
    assert [h.listener._retry_delay(n) for n in range(1, 8)] == [2, 4, 8, 16, 30, 30, 30]


def test_detector_problem_is_reported() -> None:
    def fail():
        raise WakeWordUnavailableError("The wake-word model is not downloaded yet.")

    h = Harness(noise(1.0))
    h.listener._detector_factory = fail
    try:
        assert h.run_until(lambda: h.listener.state == "error")
        assert "Hands-free listening is waiting" in h.notices[0] and "keep trying" in h.notices[0]
    finally:
        h.stop()


# ------------------------------------------------------------------ microphone


def test_microphone_stream_frames_and_lock() -> None:
    sd = FakeSoundDevice(tone(1.0, 0.2), supported_rates=(48000,))
    mic = Microphone(sd=sd)
    with mic.open_stream(FRAME_SAMPLES) as stream:
        frame = stream.read(1.0)
        assert frame is not None and frame.size == FRAME_SAMPLES and frame.dtype == np.float32
        assert stream.rate == 48000
        with pytest.raises(AudioError, match="already recording"):
            mic.record(start_timeout=0.1)
    assert sd.open_streams == 0
    mic.record(fixed_seconds=0.05)  # the lock was released


def test_disabled_microphone_never_opens() -> None:
    from app.voice.microphone import MicrophoneDisabledError

    with pytest.raises(MicrophoneDisabledError):
        Microphone(enabled=False, sd=FakeSoundDevice()).open_stream()


# ------------------------------------------------------------------ settings


def test_off_by_default_and_engine_choice() -> None:
    assert Settings().hands_free is False
    assert resolve_engine(Settings()) == "openwakeword"
    assert resolve_engine(Settings(wake_word="hey friday")) == "whisper"
    assert resolve_engine(Settings(wake_word_engine="whisper")) == "whisper"
    with pytest.raises(ValueError):
        Settings(wake_word_model="model.onnx")  # must be absolute


def test_chime_is_short_and_quiet() -> None:
    chime = wake_chime()
    assert 0.1 < chime.size / 22050 < 0.25 and np.max(np.abs(chime)) <= 0.25


# ---------------------------------------------------------------- model files


def fake_models(monkeypatch, content: bytes = b"onnx-bytes") -> list[ModelFile]:
    files = [ModelFile("melspectrogram.onnx", hashlib.sha256(content).hexdigest()),
             ModelFile("embedding_model.onnx", hashlib.sha256(content).hexdigest()),
             ModelFile("hey_jarvis_v0.1.onnx", hashlib.sha256(content).hexdigest())]
    monkeypatch.setattr(wake_word, "FEATURE_MODELS", tuple(files[:2]))
    monkeypatch.setattr(wake_word, "PRETRAINED", {"hey jarvis": files[2]})
    return files


def test_download_verifies_checksums(tmp_path: Path, monkeypatch) -> None:
    fake_models(monkeypatch)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"onnx-bytes")

    fetched = download_models(tmp_path, "hey jarvis", transport=httpx.MockTransport(handler))
    assert sorted(fetched) == ["embedding_model.onnx", "hey_jarvis_v0.1.onnx", "melspectrogram.onnx"]
    assert all(url.startswith("https://github.com/dscripka/openWakeWord/releases/download/") for url in seen)
    assert missing_models(tmp_path, "hey jarvis") == []
    assert download_models(tmp_path, "hey jarvis", transport=httpx.MockTransport(handler)) == []  # kept


def test_tampered_download_is_refused(tmp_path: Path, monkeypatch) -> None:
    fake_models(monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"something else"))
    with pytest.raises(WakeWordUnavailableError, match="checksum"):
        download_models(tmp_path, "hey jarvis", transport=transport)
    assert list(tmp_path.iterdir()) == []  # nothing half-written is left


def test_changed_model_file_is_refused(tmp_path: Path, monkeypatch) -> None:
    for model in fake_models(monkeypatch):
        (tmp_path / model.name).write_bytes(b"tampered")
    with pytest.raises(WakeWordUnavailableError, match="damaged or was changed"):
        OnnxWakeWordDetector(tmp_path)


def test_offline_mode_never_downloads(tmp_path: Path) -> None:
    with pytest.raises(WakeWordUnavailableError, match="offline"):
        prepare_detector(Settings(offline_mode=True), tmp_path, allow_download=False)


REAL_MODELS = Path(__file__).resolve().parents[1] / "data" / "models" / "wakeword"


@pytest.mark.skipif(missing_models(REAL_MODELS, "hey jarvis") != [], reason="run --setup-voice to test the real model")
def test_real_model_stays_quiet_on_noise_and_tones() -> None:
    detector = OnnxWakeWordDetector(REAL_MODELS)
    audio = np.concatenate([noise(2.0, 0.01), tone(1.0, 0.3), noise(1.0, 0.01)])
    scores = [detector.score(audio[i:i + FRAME_SAMPLES]) for i in range(0, audio.size - FRAME_SAMPLES, FRAME_SAMPLES)]
    assert max(scores) < 0.5 and all(0.0 <= s <= 1.0 for s in scores)


def test_fillers_in_the_follow_up_window_are_ignored() -> None:
    """ "Mm-hmm, mm." after a reply once made the model repeat its last action."""
    audio = np.concatenate([wake_then(tail=1.5), tone(0.6, 0.3), noise(3.0)])
    h = Harness(audio, text="open chrome", follow_up_seconds=900)
    try:
        assert h.run_until(lambda: h.results)
        h.stt.text = "Mm-hmm, mm."
        h.listener.resume(follow_up=True)
        assert not h.run_until(lambda: len(h.results) > 1, timeout=2.0)
        assert h.listener.state == "follow_up" and h.notices == []  # ignored quietly; the conversation goes on
    finally:
        h.stop()


def test_noise_phrases_in_follow_up_and_unclear_speech_stay_quiet() -> None:
    audio = np.concatenate([wake_then(tail=1.5), tone(0.6, 0.3), noise(3.0)])
    h = Harness(audio, text="open chrome")
    try:
        assert h.run_until(lambda: h.results)
        h.stt.text, h.stt.confidence = "Thank you.", 0.9
        h.listener.resume(follow_up=True)
        assert not h.run_until(lambda: len(h.results) > 1, timeout=2.0) and h.notices == []
    finally:
        h.stop()


def test_filler_after_the_wake_phrase_keeps_listening() -> None:
    h = Harness(wake_then(tail=8.0), text="umm")
    try:
        assert h.run_until(lambda: h.states.count("command") >= 2, timeout=5)  # still waiting for the command
        assert h.results == []
    finally:
        h.stop()


def test_filler_words() -> None:
    from app.voice.wake_word import is_background_noise, is_filler

    assert all(is_filler(t) for t in ("Mm.", "Mm-hmm, mm.", "Uhh...", "Hmm?", "Oh."))
    assert not any(is_filler(t) for t in ("open chrome", "Stop.", "um open chrome"))
    assert is_background_noise("Thank you.") and not is_background_noise("thank you, open notepad")



def test_near_miss_is_double_checked_with_speech_recognition() -> None:
    """ "Jarvis, are you there?" scores below the trigger; a short utterance like that is transcribed to check."""
    detector = LoudDetector()
    detector.near_miss_level = 0.4
    h = Harness(np.concatenate([noise(1.0), tone(1.2, 0.4), noise(3.0)]), text="Jarvis, are you there?",
                detector=detector, user_address="sir")
    try:
        assert h.run_until(lambda: h.results)
        assert h.results[0].status == "wake_only" and "How can I help" in h.results[0].message and h.wakes == [1]
    finally:
        h.stop()


def test_near_miss_without_the_name_is_dropped() -> None:
    detector = LoudDetector()
    detector.near_miss_level = 0.4
    h = Harness(np.concatenate([noise(1.0), tone(1.2, 0.4), noise(3.0)]), text="The service is great.", detector=detector)
    try:
        assert not h.run_until(lambda: h.results, timeout=1.5)
        assert h.wakes == [] and h.listener.state == "waiting"
    finally:
        h.stop()


def test_near_miss_command_in_one_breath() -> None:
    detector = LoudDetector()
    detector.near_miss_level = 0.4
    h = Harness(np.concatenate([noise(1.0), tone(1.2, 0.4), noise(3.0)]), text="Jervis, open Chrome.", detector=detector)
    try:
        assert h.run_until(lambda: h.results)
        assert h.results[0].ok and h.results[0].text == "open Chrome."  # "Jervis" is close enough
    finally:
        h.stop()


def test_conversation_continues_without_the_wake_phrase() -> None:
    parts = [wake_then(tail=1.0)]
    for _ in range(3):
        parts += [noise(2.0), tone(0.6, 0.3)]
    parts.append(noise(3.0))
    h = Harness(np.concatenate(parts), text="what time is it")
    try:
        for n in range(1, 5):
            assert h.run_until(lambda: len(h.results) >= n)
            h.listener.resume(follow_up=True)
        assert h.wakes == [1] and all(r.ok for r in h.results)  # woken once, four requests
    finally:
        h.stop()


def test_conversation_ends_after_quiet_time() -> None:
    h = Harness(np.concatenate([wake_then(tail=1.0), noise(12.0)]), follow_up_seconds=5)
    try:
        assert h.run_until(lambda: h.results)
        h.listener.resume(follow_up=True)
        assert h.run_until(lambda: h.states[-1] == "waiting" and "follow_up" in h.states, timeout=10)
    finally:
        h.stop()


def test_goodbye_ends_the_conversation() -> None:
    h = Harness(wake_then(), text="Okay, thank you Jarvis, that's all.", user_address="sir")
    try:
        assert h.run_until(lambda: h.results)
        result = h.results[0]
        assert result.status == "dismissed" and result.message == "Alright, sir. Just say Jarvis when you need me."
    finally:
        h.stop()


def test_what_counts_as_presence_and_goodbye() -> None:
    from app.voice.hands_free import is_dismissal, is_presence_check

    s = Settings()
    assert all(is_presence_check(t, s) for t in ("", "Are you there?", "Jarvis?", "Hello.", "Can you hear me?"))
    assert not is_presence_check("open chrome", s)
    assert all(is_dismissal(t, s) for t in ("Goodbye.", "That's all, thanks.", "Go to sleep, Jarvis.", "Stop listening"))
    assert not any(is_dismissal(t, s) for t in ("stop the music", "close chrome", "good morning"))



def test_just_the_name_wakes_it() -> None:
    """ "Jarvis?" scores low on the "hey jarvis" model; short sentences are checked for the name."""
    h = Harness(np.concatenate([noise(1.0), tone(0.8, 0.3), noise(3.0)]), text="Jarvis?", user_address="sir")
    try:
        assert h.run_until(lambda: h.results)
        assert h.results[0].status == "wake_only" and h.wakes == [1]
    finally:
        h.stop()


def test_name_with_a_command_or_question() -> None:
    for spoken, expected in (("Jarvis, are you there?", "wake_only"), ("Jervis, open YouTube.", "ok")):
        h = Harness(np.concatenate([noise(1.0), tone(1.2, 0.3), noise(3.0)]), text=spoken)
        try:
            assert h.run_until(lambda: h.results), spoken
            assert h.results[0].status == expected, spoken
        finally:
            h.stop()


def test_name_check_can_be_turned_off() -> None:
    h = Harness(np.concatenate([noise(1.0), tone(0.8, 0.3), noise(3.0)]), text="Jarvis?", wake_on_name=False)
    try:
        assert not h.run_until(lambda: h.results, timeout=1.5)
    finally:
        h.stop()


def test_long_speech_is_not_transcribed_while_waiting() -> None:
    """Only short sentences are checked: a long conversation nearby is never transcribed."""
    checked = []
    h = Harness(np.concatenate([noise(1.0), tone(4.8, 0.3), noise(3.0)]), text="Jarvis, open Chrome.")
    real = h.stt.transcribe
    h.stt.transcribe = lambda audio, language=None: checked.append(audio.size) or real(audio, language)
    try:
        assert not h.run_until(lambda: h.results, timeout=1.5)
        assert checked == []
    finally:
        h.stop()
