"""Natural neural voices (Microsoft Edge "read aloud" voices) for Bangla and English.

Every sentence is spoken with the voice of its language: Bengali script -> the Bangla
voice (bn-BD), anything else -> the English voice. Delivery follows the sentence a
little, so replies sound less flat: exclamations are brighter, apologies softer.

This needs the internet, and the text of each reply is sent to Microsoft's speech
service. When it isn't reachable, the offline engine (Piper / Windows voice) speaks
instead, with the offline Bangla voice for Bangla sentences once it is downloaded.
"""

from __future__ import annotations

import asyncio
import io
import re
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from app.tts.base import AudioClip, TTSEngine, TTSError
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tts.neural")
BENGALI = re.compile(r"[ঀ-৿]")
SAMPLE_RATE = 24000
OFFLINE_RETRY_SECONDS = 60.0
_SOFT = re.compile(r"\b(sorry|unfortunately|afraid|couldn't|can't|failed|problem|error)\b|দুঃখিত|পারিনি|সমস্যা|ভুল",
                   re.I)
_BRIGHT = re.compile(r"!|\b(great|awesome|done|ready|welcome|good morning|congratulations)\b|দারুণ|চমৎকার|শুভ|হয়ে গেছে",
                     re.I)


def prosody(text: str, speed: float = 1.0, rate_offset: int = 0, pitch_offset: int = 0,
            gentle: bool = False) -> tuple[str, str]:
    """(rate, pitch) for a sentence, e.g. ("+5%", "+4Hz"). Offsets shape a voice profile. ``gentle``: the small,
    human-sized changes of tone (a softer sentence a little slower, good news a little brighter), not big jumps."""
    rate, pitch = round((speed - 1.0) * 100) + rate_offset, pitch_offset
    soft, bright = ((-3, -1), (+2, +1)) if gentle else ((-6, -3), (+4, +4))
    if _SOFT.search(text):
        rate, pitch = rate + soft[0], pitch + soft[1]
    elif _BRIGHT.search(text):
        rate, pitch = rate + bright[0], pitch + bright[1]
    return f"{rate:+d}%", f"{pitch:+d}Hz"


def decode_mp3(data: bytes) -> np.ndarray:
    """MP3 bytes -> float32 mono at SAMPLE_RATE (PyAV, which faster-whisper already brings)."""
    import av

    samples: list[np.ndarray] = []
    with av.open(io.BytesIO(data)) as container:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                samples.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            samples.append(out.to_ndarray().reshape(-1))
    return np.concatenate(samples).astype(np.float32) if samples else np.zeros(0, np.float32)


class NeuralTTS(TTSEngine):
    name = "neural"

    def __init__(self, english_voice: str, bangla_voice: str, *, fallback: TTSEngine | None = None,
                 synthesize_mp3: Callable[[str, str, str, str], bytes] | None = None,
                 decoder: Callable[[bytes], np.ndarray] = decode_mp3, rate_offset: int = 0, gentle: bool = False,
                 pitch_offset: int = 0) -> None:
        self.rate_offset, self.pitch_offset = rate_offset, pitch_offset  # English sentences only
        self.gentle = gentle
        self.english_voice = english_voice
        self.bangla_voice = bangla_voice
        self.fallback = fallback
        self._synthesize_mp3 = synthesize_mp3 or self._edge_mp3
        self._decode = decoder
        self._offline_until = 0.0

    @property
    def description(self) -> str:
        return f"Neural voice ({self.english_voice} / {self.bangla_voice})"

    def load(self) -> None:
        if self._synthesize_mp3 == self._edge_mp3:
            try:
                import edge_tts  # noqa: F401
            except ImportError as exc:
                raise TTSError("The neural voice needs the 'edge-tts' package: pip install -r requirements.txt") from exc
        if self.fallback is not None:
            try:
                self.fallback.load()
            except TTSError as exc:
                log_event(_log, "tts_fallback_unavailable", reason=str(exc))
                self.fallback = None

    @staticmethod
    def _edge_mp3(text: str, voice: str, rate: str, pitch: str) -> bytes:
        import edge_tts

        async def run() -> bytes:
            data = bytearray()
            communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    data.extend(chunk["data"])
            return bytes(data)

        return asyncio.run(asyncio.wait_for(run(), timeout=15))

    def voice_for(self, text: str) -> str:
        return self.bangla_voice if BENGALI.search(text) else self.english_voice

    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        bangla = bool(BENGALI.search(text))
        if time.monotonic() >= self._offline_until:
            rate, pitch = prosody(text, speed, 0 if bangla else self.rate_offset, 0 if bangla else self.pitch_offset,
                                  gentle=self.gentle)
            try:
                data = self._synthesize_mp3(text, self.voice_for(text), rate, pitch)
                samples = self._decode(data)
                if samples.size:
                    return AudioClip(samples, SAMPLE_RATE)
                raise TTSError("empty audio")
            except Exception as exc:  # no internet, service changed, bad audio...
                self._offline_until = time.monotonic() + OFFLINE_RETRY_SECONDS
                log_event(_log, "neural_voice_unavailable", detail=str(exc)[:200])
        if self.fallback is not None and (not bangla or getattr(self.fallback, "speaks_bangla", False)):
            return self.fallback.synthesize(text, speed)
        return AudioClip(np.zeros(0, np.float32), SAMPLE_RATE)  # Bangla offline: shown, not spoken

    def close(self) -> None:
        if self.fallback is not None:
            self.fallback.close()


def voices(language_prefix: str = "") -> list[dict[str, Any]]:
    """The neural voices (needs the internet), e.g. voices("bn-")."""
    import edge_tts

    found = asyncio.run(edge_tts.list_voices())
    return [v for v in found if v.get("ShortName", "").startswith(language_prefix)]
