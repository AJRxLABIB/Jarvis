"""Tone awareness: notice when you sound in a hurry or tired, and answer accordingly.

Only two simple things are measured, on this PC: how fast you speak (words per second) and how
loud (dB). They are compared with YOUR usual values (learned from your own requests), so a fast
or quiet speaker is not misjudged. "Rushed" -> the next answer is extra short; "tired" -> short
and gentle. Nothing about your voice is kept except the two running averages (data\\tone.json).
It is a hint, not a diagnosis.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

FILE = "tone.json"
MIN_SAMPLES = 8  # requests before it judges anything
MIN_WORDS = 4
ALPHA = 0.1  # how fast the "usual" values follow you
HINTS = {
    "rushed": "The user sounds in a hurry: answer in one short sentence, no extras.",
    "tired": "The user sounds tired: keep the answer short, calm and gentle.",
}


def measure(audio: np.ndarray, text: str, rate: int = 16000) -> tuple[float, float] | None:
    """(words per second, loudness in dBFS of the speech) or None if too short to tell."""
    audio = np.asarray(audio, np.float32).reshape(-1)
    words = len(text.split())
    if words < MIN_WORDS or audio.size < rate * 0.8:
        return None
    hop = rate // 50
    frames = audio[: audio.size // hop * hop].reshape(-1, hop)
    level = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    voiced = level > level.max() - 30
    if voiced.sum() < 10:
        return None
    speaking = voiced.sum() * hop / rate  # seconds with speech (pauses don't count)
    return words / speaking, float(np.median(level[voiced]))


class ToneTracker:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.path = Path(data_dir) / FILE if data_dir is not None else None
        self.speed, self.loud, self.samples = 0.0, 0.0, 0
        if self.path is not None and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.speed, self.loud, self.samples = float(data["speed"]), float(data["loud"]), int(data["samples"])
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def observe(self, audio: np.ndarray, text: str) -> str | None:
        """Judge this request against your usual way of speaking, then learn from it. None = normal / unsure."""
        measured = measure(audio, text)
        if measured is None:
            return None
        speed, loud = measured
        tone = None
        if self.samples >= MIN_SAMPLES:
            if speed > self.speed * 1.35 and loud > self.loud + 2:
                tone = "rushed"
            elif speed < self.speed * 0.72 and loud < self.loud - 4:
                tone = "tired"
        if self.samples == 0:
            self.speed, self.loud = speed, loud
        else:
            self.speed += ALPHA * (speed - self.speed)
            self.loud += ALPHA * (loud - self.loud)
        self.samples += 1
        self._save()
        return tone

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.write_text(json.dumps({"speed": round(self.speed, 3), "loud": round(self.loud, 2),
                                             "samples": self.samples}), encoding="utf-8")
        except OSError:
            pass
