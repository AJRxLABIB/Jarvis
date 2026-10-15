"""A fake ``sounddevice`` module: scripted microphone input, captured speaker output."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import numpy as np

RATE = 16_000
FRAME = int(RATE * 0.03)


def tone(seconds: float, level: float = 0.3, rate: int = RATE) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def noise(seconds: float, level: float = 0.002, rate: int = RATE, seed: int = 1) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(int(seconds * rate)) * level).astype(np.float32)


class FakeSoundDevice:
    def __init__(self, input_audio: np.ndarray | None = None, *, supported_rates: tuple[int, ...] = (16000, 22050, 48000),
                 realtime: float = 0.0) -> None:
        self.input_audio = input_audio if input_audio is not None else np.zeros(0, dtype=np.float32)
        self.supported_rates = supported_rates
        self.realtime = realtime  # seconds to sleep per frame (0 = as fast as possible)
        self.default = SimpleNamespace(device=(1, 3), hostapi=0)
        self.opened_inputs: list[dict[str, Any]] = []
        self.played: list[np.ndarray] = []
        self.output_rates: list[int] = []
        self.open_streams = 0
        self._devices = [
            {"name": "Microsoft Sound Mapper - Input", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0, "default_samplerate": 44100.0},
            {"name": "Microphone (USB Audio)", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000.0},
            {"name": "Microphone (USB Audio)", "hostapi": 1, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000.0},
            {"name": "Speakers (Realtek Audio)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
        ]

    def query_hostapis(self, index: int | None = None) -> Any:
        apis = [{"name": "MME"}, {"name": "Windows WASAPI"}]
        return apis if index is None else apis[index]

    def query_devices(self, device: int | None = None, kind: str | None = None) -> Any:
        if device is None and kind is None:
            return self._devices
        index = device if device is not None else (self.default.device[0] if kind == "input" else self.default.device[1])
        return self._devices[index]

    def InputStream(self, **kwargs: Any) -> "FakeInputStream":  # noqa: N802
        if kwargs["samplerate"] not in self.supported_rates:
            raise RuntimeError("Invalid sample rate")
        self.opened_inputs.append(kwargs)
        return FakeInputStream(self, **kwargs)

    def OutputStream(self, **kwargs: Any) -> "FakeOutputStream":  # noqa: N802
        if kwargs["samplerate"] not in self.supported_rates:
            raise RuntimeError("Invalid sample rate")
        self.output_rates.append(kwargs["samplerate"])
        return FakeOutputStream(self)


class FakeInputStream:
    def __init__(self, sd: FakeSoundDevice, *, samplerate: int, blocksize: int, callback: Any, **_: Any) -> None:
        self.sd, self.rate, self.block, self.callback = sd, samplerate, blocksize, callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.sd.open_streams += 1
        audio = self.sd.input_audio
        if self.rate != RATE and audio.size:  # crude: repeat samples to emulate a higher device rate
            audio = np.repeat(audio, self.rate // RATE)

        def feed() -> None:
            pos = 0
            while not self._stop.is_set():
                chunk = audio[pos:pos + self.block]
                if chunk.size < self.block:
                    chunk = np.concatenate([chunk, np.zeros(self.block - chunk.size, dtype=np.float32)])
                pos += self.block
                self.callback(chunk.reshape(-1, 1), self.block, None, None)
                time.sleep(self.sd.realtime or 0.0005)

        self._thread = threading.Thread(target=feed, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(1)

    def close(self) -> None:
        self.sd.open_streams -= 1


class FakeOutputStream:
    def __init__(self, sd: FakeSoundDevice) -> None:
        self.sd = sd
        self.aborted = False

    def __enter__(self) -> "FakeOutputStream":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def write(self, data: np.ndarray) -> None:
        self.sd.played.append(data.ravel().copy())
        if self.sd.realtime:
            time.sleep(self.sd.realtime)

    def abort(self) -> None:
        self.aborted = True
