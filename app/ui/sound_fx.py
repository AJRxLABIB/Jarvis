"""Interface sound effects (UI_SOUNDS=true): short synthesized tones, made here with numpy - no sound files, nothing
downloaded. They play through the speech player's "interface sound" path, so they never interrupt or queue behind
speech, and they stay silent while Jarvis speaks or listens (the microphone must not hear them).

Sounds: ``tool`` (a soft tick when an action runs), ``attention`` (two rising notes: something waits for your answer),
``done`` (a short bright chord after a reply), ``error`` (a low double buzz).
"""

from __future__ import annotations

import time

import numpy as np

RATE = 22050
KINDS = ("tool", "attention", "done", "error")
MIN_GAP = {"tool": 0.35, "attention": 1.0, "done": 0.8, "error": 1.0}   # never a machine-gun of ticks


def _tone(freqs, seconds: float, attack: float = 0.004, decay: float = 6.0) -> np.ndarray:
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    wave = sum(np.sin(2 * np.pi * f * t) for f in freqs) / max(1, len(freqs))
    env = np.minimum(1.0, t / max(attack, 1e-4)) * np.exp(-t * decay)
    return (wave * env).astype(np.float32)


def synth(kind: str) -> np.ndarray:
    if kind == "tool":
        out = _tone((1760.0,), 0.07, decay=55.0) * 0.35
    elif kind == "attention":
        out = np.concatenate([_tone((880.0,), 0.11, decay=18.0), _tone((1318.5,), 0.16, decay=12.0)]) * 0.45
    elif kind == "done":
        out = _tone((1046.5, 1318.5, 1568.0), 0.28, decay=11.0) * 0.4
    elif kind == "error":
        buzz = _tone((196.0, 207.7), 0.12, decay=14.0)
        out = np.concatenate([buzz, np.zeros(int(RATE * 0.05), np.float32), buzz]) * 0.5
    else:
        raise ValueError(f"unknown sound {kind!r}")
    fade = min(out.size, int(RATE * 0.01))                     # no click at the end
    if fade:
        out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return np.clip(out, -1.0, 1.0).astype(np.float32)


class SoundFx:
    """Decides whether a sound may play now and plays it. ``busy()`` -> True while speaking or listening."""

    def __init__(self, play, enabled=lambda: False, busy=lambda: False, volume=lambda: 0.5, clock=time.monotonic):
        self._play, self._enabled, self._busy, self._volume, self._clock = play, enabled, busy, volume, clock
        self._last: dict[str, float] = {}
        self._cache: dict[str, np.ndarray] = {}

    def play(self, kind: str) -> bool:
        if kind not in KINDS or not self._enabled() or self._busy():
            return False
        now = self._clock()
        if now - self._last.get(kind, -1e9) < MIN_GAP[kind]:
            return False
        self._last[kind] = now
        samples = self._cache.get(kind)
        if samples is None:
            samples = self._cache[kind] = synth(kind)
        from app.tts.base import AudioClip

        level = max(0.0, min(1.0, float(self._volume())))
        self._play(AudioClip((samples * level).astype(np.float32), RATE))
        return True
