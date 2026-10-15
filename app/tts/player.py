"""Audio playback that can be interrupted within ~50 ms."""

from __future__ import annotations

import threading
from typing import Any, Protocol

import numpy as np

from app.tts.base import AudioClip
from app.voice.audio import AudioDeviceError, import_sounddevice, resample, resolve_device

_CHUNK_SECONDS = 0.05


class Player(Protocol):
    def play(self, clip: AudioClip, *, volume: float, abort: threading.Event) -> bool:
        """Play a clip; return False if aborted."""
        ...


class SoundDevicePlayer:
    def __init__(self, device: str = "", sd: Any = None) -> None:
        self.device = device
        self._sd = sd

    def play(self, clip: AudioClip, *, volume: float, abort: threading.Event) -> bool:
        if clip.samples.size == 0:
            return True
        sd = self._sd or import_sounddevice()
        device = resolve_device(self.device, "output", sd)
        samples = np.clip(clip.samples * volume, -1.0, 1.0).astype(np.float32)
        rate = clip.sample_rate
        try:
            stream = sd.OutputStream(device=device, samplerate=rate, channels=1, dtype="float32")
        except Exception:
            try:  # device does not accept this rate: resample to its default
                rate = int(sd.query_devices(device, "output")["default_samplerate"])
                samples = resample(samples, clip.sample_rate, rate)
                stream = sd.OutputStream(device=device, samplerate=rate, channels=1, dtype="float32")
            except Exception as exc:
                raise AudioDeviceError(f"Could not open the speakers: {exc}") from exc
        chunk = max(1, int(rate * _CHUNK_SECONDS))
        with stream:
            for start in range(0, samples.size, chunk):
                if abort.is_set():
                    stream.abort()
                    return False
                stream.write(samples[start:start + chunk].reshape(-1, 1))
        return True
