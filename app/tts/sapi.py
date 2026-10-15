"""Windows built-in speech (SAPI 5) as a zero-download fallback.

Speech is rendered into memory (not straight to the speakers) so it goes
through the same queue, volume and stop handling as Piper. COM objects are
per-thread, so they are created in the thread that synthesizes.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import numpy as np

from app.tts.base import AudioClip, TTSEngine, TTSError
from app.utils.logging_setup import get_logger

_log = get_logger("tts.sapi")
_FORMAT_22KHZ_16BIT_MONO = 22  # SpeechAudioFormatType.SAFT22kHz16BitMono
_SAMPLE_RATE = 22050
_FLAG_IS_NOT_XML = 16  # SVSFIsNotXML: never interpret text as SAPI XML markup


class SapiTTS(TTSEngine):
    name = "sapi"

    def __init__(self, voice_name: str = "") -> None:
        self.voice_name = voice_name
        self._local = threading.local()
        self._selected: str | None = None

    @property
    def description(self) -> str:
        return f"Windows voice ({self._selected or 'default'})"

    def _modules(self) -> tuple[Any, Any]:
        if sys.platform != "win32":
            raise TTSError("Windows speech is only available on Windows.")
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise TTSError("pywin32 is not installed. Run 'pip install -r requirements.txt'.") from exc
        return pythoncom, win32com.client

    def _voice(self) -> Any:
        voice = getattr(self._local, "voice", None)
        if voice is not None:
            return voice
        pythoncom, client = self._modules()
        pythoncom.CoInitialize()
        try:
            voice = client.Dispatch("SAPI.SpVoice")
        except Exception as exc:
            raise TTSError(f"Windows speech is not available: {exc}") from exc
        if self.voice_name:
            wanted = self.voice_name.lower()
            tokens = voice.GetVoices()
            for i in range(tokens.Count):
                token = tokens.Item(i)
                if wanted in token.GetDescription().lower():
                    voice.Voice = token
                    break
            else:
                _log.warning("Windows voice %r not found; using the default voice", self.voice_name)
        self._selected = voice.Voice.GetDescription()
        self._local.voice = voice
        return voice

    def load(self) -> None:
        self._voice()

    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        voice = self._voice()
        _, client = self._modules()
        try:
            audio_format = client.Dispatch("SAPI.SpAudioFormat")
            audio_format.Type = _FORMAT_22KHZ_16BIT_MONO
            stream = client.Dispatch("SAPI.SpMemoryStream")
            stream.Format = audio_format
            voice.AudioOutputStream = stream
            voice.Rate = max(-10, min(10, int(round((speed - 1.0) * 10))))
            voice.Speak(text, _FLAG_IS_NOT_XML)
            data = bytes(stream.GetData())
        except Exception as exc:
            _log.warning("SAPI synthesis failed", exc_info=True)
            raise TTSError("Windows speech synthesis failed. The details are in the log.") from exc
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        return AudioClip(samples, _SAMPLE_RATE)

    def voices(self) -> list[str]:
        tokens = self._voice().GetVoices()
        return [tokens.Item(i).GetDescription() for i in range(tokens.Count)]
