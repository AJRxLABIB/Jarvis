"""The JARVIS voice: Jarvis's own voice profile - a calm, measured British male voice with a subtle "AI in the
room" finish.

It is NOT a copy of any actor's voice (that would mean cloning a real person's voice from films without their
consent). It's built from licensed synthetic voices:

* online  : Microsoft's neural en-GB-RyanNeural, a touch slower and lower than normal;
* offline : Kokoro's British male voice (bm_george) when it's downloaded, else the Windows voice;

and then a finish in numpy. The default ("natural") is what a professional voice recording gets: rumble removed,
a gentle compressor so quiet and loud words sit together, an even loudness and soft edges - nothing added to the voice
itself, so it sounds like a person talking to you. The old "classic" finish (short room reflections plus a doubled
"shimmer" copy) is kept for anyone who wants the synthetic edge back (TTS_STYLE=classic); those comb-filter echoes are
what made it sound robotic. Bangla sentences are spoken by the Bangla voice (the British voice can't speak Bangla), with
the same finish so the two sound like one assistant.
"""

from __future__ import annotations

import re

import numpy as np

from app.tts.base import AudioClip, TTSEngine

ENGLISH_VOICE = "en-GB-RyanNeural"
OFFLINE_VOICE = "bm_george"
RATE_OFFSET, PITCH_OFFSET = -2, 0   # percent, Hz: the voice's own natural pitch, a touch unhurried
CLASSIC_RATE_OFFSET, CLASSIC_PITCH_OFFSET = -6, -3
BENGALI = re.compile(r"[ঀ-৿]")

# early reflections (delay ms, gain) - a small, clean room; no long echo
REFLECTIONS = ((11.0, 0.16), (23.0, 0.11), (37.0, 0.07), (53.0, 0.045))
SHIMMER_MS, SHIMMER_GAIN = 6.0, 0.08  # a very short doubled copy: the faint "synthetic" edge
LOW_CUT_HZ = 110.0
PEAK = 0.89


def low_cut(samples: np.ndarray, rate: int, cutoff: float = LOW_CUT_HZ) -> np.ndarray:
    """High-pass by subtracting a moving average (vectorised): removes rumble, makes the voice crisper."""
    if samples.size == 0:
        return samples
    window = max(3, int(rate / cutoff))
    padded = np.concatenate([np.full(window // 2, samples[0]), samples, np.full(window - window // 2 - 1, samples[-1])])
    csum = np.cumsum(np.concatenate([[0.0], padded.astype(np.float64)]))
    average = (csum[window:] - csum[:-window]) / window
    return (samples - average[: samples.size]).astype(np.float32)


def jarvis_effect(samples: np.ndarray, rate: int) -> np.ndarray:
    """The finish: low cut + early reflections + shimmer, peak-normalised. Output is a little longer (the tail)."""
    dry = np.asarray(samples, dtype=np.float32)
    if dry.size == 0 or rate <= 0:
        return dry
    dry = low_cut(dry, rate)
    tail = int(rate * (max(d for d, _ in REFLECTIONS) + 5) / 1000)
    wet = np.zeros(dry.size + tail, dtype=np.float32)
    wet[:dry.size] += dry
    shift = int(rate * SHIMMER_MS / 1000)
    wet[shift:shift + dry.size] += SHIMMER_GAIN * dry
    for delay_ms, gain in REFLECTIONS:
        shift = int(rate * delay_ms / 1000)
        wet[shift:shift + dry.size] += gain * dry
    peak = float(np.max(np.abs(wet))) if wet.size else 0.0
    if peak > 1e-6:
        wet *= PEAK / peak
    return wet.astype(np.float32)


TARGET_RMS_DB = -19.0          # even, comfortable loudness (about broadcast level)
COMPRESS_ABOVE_DB, RATIO = -24.0, 2.2
FADE_MS = 12.0


def natural_finish(samples: np.ndarray, rate: int) -> np.ndarray:
    """A clean, professional finish that keeps the voice human: 75 Hz low cut, soft-knee compression on a 30 ms
    loudness envelope, loudness evened to TARGET_RMS_DB with a peak ceiling, and short fades (no clicks)."""
    voice = np.asarray(samples, dtype=np.float32)
    if voice.size == 0 or rate <= 0:
        return voice
    voice = low_cut(voice, rate, 75.0)
    window = max(1, int(rate * 0.03))
    power = np.convolve(voice.astype(np.float64) ** 2, np.ones(window) / window, mode="same")
    level_db = 10 * np.log10(np.maximum(power, 1e-10))
    over = np.maximum(0.0, level_db - COMPRESS_ABOVE_DB)
    gain_db = -over * (1 - 1 / RATIO)
    smooth = max(1, int(rate * 0.012))                                 # no pumping: the gain moves gently
    gain_db = np.convolve(gain_db, np.ones(smooth) / smooth, mode="same")
    voice = (voice * 10 ** (gain_db / 20)).astype(np.float32)
    rms = float(np.sqrt(np.mean(np.square(voice, dtype=np.float64))))
    if rms > 1e-6:
        voice *= 10 ** (TARGET_RMS_DB / 20) / rms
    peak = float(np.max(np.abs(voice)))
    if peak > PEAK:
        voice *= PEAK / peak
    fade = min(int(rate * FADE_MS / 1000), voice.size // 4)
    if fade > 1:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        voice[:fade] *= ramp
        voice[-fade:] *= ramp[::-1]
    return voice.astype(np.float32)


class JarvisVoice(TTSEngine):
    """Wraps the engine that makes the words; adds the JARVIS finish."""

    name = "jarvis"

    def __init__(self, inner: TTSEngine, *, effect: bool = True, style: str = "natural") -> None:
        self.inner = inner
        self.effect = effect
        self.style = style if style in ("natural", "classic") else "natural"
        self.speaks_bangla = getattr(inner, "speaks_bangla", False)

    @property
    def description(self) -> str:
        return f"JARVIS voice ({self.inner.description})"

    def load(self) -> None:
        self.inner.load()

    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        clip = self.inner.synthesize(text, speed)
        if not self.effect or clip.samples.size == 0:
            return clip
        finish = jarvis_effect if self.style == "classic" else natural_finish
        return AudioClip(finish(clip.samples, clip.sample_rate), clip.sample_rate)

    def voices(self) -> list[str]:
        return ["JARVIS"]

    def close(self) -> None:
        self.inner.close()
