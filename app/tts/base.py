"""Text-to-speech interface: engines turn text into audio; the manager plays it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


class TTSError(Exception):
    """Text-to-speech problem with a user-readable message."""


@dataclass(frozen=True)
class AudioClip:
    samples: np.ndarray  # float32 mono, -1..1
    sample_rate: int

    @property
    def duration(self) -> float:
        return self.samples.size / self.sample_rate if self.sample_rate else 0.0


class TTSEngine(ABC):
    name = "base"

    @abstractmethod
    def load(self) -> None:
        """Prepare the engine. Raises TTSError if it cannot work on this machine."""

    @abstractmethod
    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        """Convert text to audio. ``speed`` > 1 speaks faster."""

    def voices(self) -> list[str]:
        return []

    @property
    def description(self) -> str:
        return self.name

    def close(self) -> None:
        pass
