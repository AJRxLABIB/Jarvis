"""Voice pipeline: microphone -> speech recognition -> conversation -> speech.

``listen()`` is blocking (run it in a worker thread); ``respond()`` is async
and starts speaking each sentence as soon as the model has produced it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.agent.conversation import AssistantReply, ConversationManager
from app.config.settings import Settings
from app.tts.manager import TTSManager
from app.tts.text import SentenceSplitter
from app.utils.logging_setup import get_logger, log_event
from app.voice.audio import AudioError
from app.voice.microphone import Microphone, Recording
from app.voice.speech_to_text import SpeechToText, STTError, Transcript
from app.voice.wake_word import is_filler, strip_wake_phrase

_log = get_logger("voice")

ListenStatus = Literal["ok", "no_speech", "wake_only", "low_confidence", "dismissed", "error"]


@dataclass(frozen=True)
class ListenResult:
    status: ListenStatus
    text: str = ""
    message: str = ""  # what to tell the user when status != "ok"
    transcript: Transcript | None = None
    recording: Recording | None = None
    audio: object = None  # the command's audio (for tone awareness); never stored

    @property
    def ok(self) -> bool:
        return self.status == "ok"


LOW_RESOURCE_LANGUAGES = {"bn", "hi", "ur", "ta", "te", "ne", "si", "mr", "gu", "pa"}


def min_confidence(settings, transcript) -> float:
    """Whisper is less sure of itself in Bangla (and similar languages) even when it is right."""
    factor = 0.6 if getattr(transcript, "language", None) in LOW_RESOURCE_LANGUAGES else 1.0
    return settings.stt_min_confidence * factor


class VoicePipeline:
    def __init__(
        self,
        settings: Settings,
        microphone: Microphone,
        stt: SpeechToText,
        tts: TTSManager,
        conversation: ConversationManager,
        vad_factory=None,
    ) -> None:
        self.vad_factory = vad_factory  # e.g. lambda: create_vad("auto"); None = energy detection
        self.settings = settings
        self.microphone = microphone
        self.stt = stt
        self.tts = tts
        self.conversation = conversation

    def listen(self, stop_event: threading.Event | None = None) -> ListenResult:
        s = self.settings
        try:
            recording = self.microphone.record(
                silence_timeout=s.silence_timeout,
                start_timeout=s.speech_start_timeout,
                max_seconds=s.max_record_seconds,
                stop_event=stop_event,
                vad=self.vad_factory() if self.vad_factory is not None else None,
            )
        except AudioError as exc:
            return ListenResult("error", message=str(exc))
        if not recording.speech_detected or recording.audio.size == 0:
            return ListenResult("no_speech", message="I didn't hear anything.", recording=recording)
        try:
            transcript = self.stt.transcribe(recording.audio, s.voice_language)
        except STTError as exc:
            return ListenResult("error", message=str(exc), recording=recording)
        if transcript.is_empty:
            return ListenResult("no_speech", message="I didn't catch that.", transcript=transcript, recording=recording)

        text, had_wake = strip_wake_phrase(transcript.text, s.wake_word)
        if had_wake and not text:
            return ListenResult("wake_only", message="Yes?", transcript=transcript, recording=recording)
        if is_filler(text):  # "mm", "hmm": never sent to the model
            return ListenResult("no_speech", message="I didn't catch a request.", transcript=transcript,
                                recording=recording)
        if transcript.confidence < min_confidence(s, transcript):
            log_event(_log, "low_confidence_transcript", confidence=round(transcript.confidence, 3))
            return ListenResult(
                "low_confidence", text=text, message="Sorry, I didn't catch that. Please say it again.",
                transcript=transcript, recording=recording,
            )
        return ListenResult("ok", text=text, transcript=transcript, recording=recording)

    async def respond(self, text: str, on_text: Callable[[str], None] | None = None) -> AssistantReply:
        splitter = SentenceSplitter()

        def on_delta(delta: str) -> None:
            if on_text is not None:
                on_text(delta)
            for sentence in splitter.feed(delta):
                self.tts.speak(sentence)

        reply = await self.conversation.send(text, on_delta=on_delta)
        rest = splitter.flush()
        if reply.ok and rest:
            self.tts.speak(rest)
        elif not reply.ok:
            self.tts.speak(reply.text)
        return reply
