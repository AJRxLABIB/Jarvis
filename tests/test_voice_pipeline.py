from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np

from app.agent.conversation import AssistantReply
from app.config.settings import Settings
from app.utils.async_runner import AsyncRunner
from app.voice.microphone import Microphone
from app.voice.pipeline import VoicePipeline
from app.voice.speech_to_text import SpeechToText, Transcript
from tests.fake_audio import FakeSoundDevice, noise, tone
from tests.test_tts import FakeEngine, FakePlayer
from app.tts.manager import TTSManager


class FakeSTT(SpeechToText):
    def __init__(self, text: str, confidence: float = 0.9) -> None:
        self.text, self.confidence = text, confidence

    @property
    def loaded(self) -> bool:
        return True

    def load(self) -> None:
        pass

    def transcribe(self, audio, language=None) -> Transcript:
        return Transcript(self.text, "en", self.confidence, 0.01, audio.size / 16000, 5)


class FakeConversation:
    def __init__(self, parts: list[str], ok: bool = True) -> None:
        self.parts, self.ok, self.received = parts, ok, []

    async def send(self, text, on_delta=None) -> AssistantReply:
        self.received.append(text)
        if not self.ok:
            return AssistantReply("Ollama is not running. Start Ollama and try again.", ok=False, error="provider_unavailable")
        for p in self.parts:
            on_delta(p)
        return AssistantReply("".join(self.parts), ok=True)


def build(stt_text: str, confidence: float = 0.9, audio=None, parts=("Opening Chrome. ", "Done"), ok=True):
    settings = Settings(silence_timeout=0.4, speech_start_timeout=1.0)
    audio = audio if audio is not None else np.concatenate([noise(0.3), tone(0.8), noise(2.0)])
    engine = FakeEngine()
    tts = TTSManager(settings, Path("."), engine=engine, player=FakePlayer())
    conv = FakeConversation(list(parts), ok)
    pipeline = VoicePipeline(settings, Microphone(sd=FakeSoundDevice(audio)), FakeSTT(stt_text, confidence), tts, conv)
    return pipeline, engine, conv, tts


def test_listen_strips_wake_word() -> None:
    pipeline, *_, tts = build("Hey Jarvis, open Chrome.")
    result = pipeline.listen()
    assert result.ok and result.text == "open Chrome."
    tts.close()


def test_listen_statuses() -> None:
    for text, conf, audio, status in (
        ("Hey Jarvis.", 0.9, None, "wake_only"),
        ("open crome", 0.1, None, "low_confidence"),
        ("", 0.9, None, "no_speech"),
        ("x", 0.9, noise(3.0), "no_speech"),
    ):
        pipeline, *_, tts = build(text, conf, audio)
        assert pipeline.listen().status == status, text
        tts.close()


def test_respond_speaks_sentences_as_they_stream() -> None:
    pipeline, engine, conv, tts = build("open chrome")
    shown: list[str] = []
    reply = asyncio.run(pipeline.respond("open chrome", on_text=shown.append))
    assert tts.wait_until_idle(5)
    assert reply.ok and engine.spoken == ["Opening Chrome.", "Done"] and "".join(shown) == "Opening Chrome. Done"
    tts.close()


def test_respond_speaks_error_message() -> None:
    pipeline, engine, _, tts = build("hi", ok=False)
    asyncio.run(pipeline.respond("hi"))
    assert tts.wait_until_idle(5) and engine.spoken == ["Ollama is not running. Start Ollama and try again."]
    tts.close()


def test_async_runner() -> None:
    async def add(a, b):
        await asyncio.sleep(0.01)
        return a + b

    with AsyncRunner() as runner:
        assert runner.run(add(2, 3), timeout=5) == 5
        future = runner.submit(asyncio.sleep(10))
        future.cancel()
    assert future.cancelled()


def test_push_to_talk_filler_is_not_sent() -> None:
    from app.config.settings import Settings
    from app.voice.microphone import Microphone
    from app.voice.pipeline import VoicePipeline
    from tests.fake_audio import FakeSoundDevice, noise, tone
    import numpy as np

    mic = Microphone(sd=FakeSoundDevice(np.concatenate([noise(0.3), tone(0.8), noise(2.0)])))
    pipeline = VoicePipeline(Settings(silence_timeout=0.4), mic, FakeSTT("Mm-hmm."), None, None)
    result = pipeline.listen()
    assert result.status == "no_speech" and not result.ok
