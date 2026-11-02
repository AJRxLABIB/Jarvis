"""Audio helpers shared by recording and playback.

``sounddevice`` is imported lazily so the rest of the app (and the tests) work
on machines without audio hardware or PortAudio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal

import numpy as np

SAMPLE_RATE = 16_000  # what speech recognition expects
DeviceKind = Literal["input", "output"]


class AudioError(Exception):
    """Audio problem with a user-readable message."""


class AudioUnavailableError(AudioError):
    pass


class AudioDeviceError(AudioError):
    pass


def import_sounddevice() -> ModuleType:
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise AudioUnavailableError(
            "Audio support is not available. Run 'pip install -r requirements.txt'. "
            f"(details: {exc})"
        ) from exc
    return sounddevice


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    is_default_input: bool
    is_default_output: bool


def _default_indices(sd: Any) -> tuple[int, int]:
    try:
        default = sd.default.device
        return int(default[0]), int(default[1])
    except (TypeError, ValueError, IndexError):
        return -1, -1


def list_devices(kind: DeviceKind | None = None, sd: Any = None) -> list[DeviceInfo]:
    sd = sd or import_sounddevice()
    default_in, default_out = _default_indices(sd)
    hostapis = sd.query_hostapis()
    devices: list[DeviceInfo] = []
    for index, dev in enumerate(sd.query_devices()):
        info = DeviceInfo(
            index=index,
            name=str(dev["name"]),
            host_api=str(hostapis[dev["hostapi"]]["name"]),
            max_input_channels=int(dev["max_input_channels"]),
            max_output_channels=int(dev["max_output_channels"]),
            default_samplerate=float(dev["default_samplerate"]),
            is_default_input=index == default_in,
            is_default_output=index == default_out,
        )
        if kind == "input" and info.max_input_channels < 1:
            continue
        if kind == "output" and info.max_output_channels < 1:
            continue
        devices.append(info)
    return devices


def resolve_device(spec: str, kind: DeviceKind, sd: Any = None) -> int | None:
    """``""`` -> None (Windows default), a number -> that index, text -> name match.

    Windows lists each device once per host API (MME, DirectSound, WASAPI ...);
    for a name match the default host API is preferred.
    """
    spec = spec.strip()
    if not spec:
        return None
    sd = sd or import_sounddevice()
    devices = list_devices(kind, sd)
    label = "microphone" if kind == "input" else "speaker"
    if spec.isdigit():
        index = int(spec)
        if any(d.index == index for d in devices):
            return index
        raise AudioDeviceError(f"There is no {label} with index {index}. Run 'python run.py --devices'.")
    matches = [d for d in devices if spec.lower() in d.name.lower()]
    if not matches:
        raise AudioDeviceError(f"No {label} matching '{spec}' was found. Run 'python run.py --devices'.")
    try:
        default_api = str(sd.query_hostapis(sd.default.hostapi)["name"])
    except Exception:
        default_api = ""
    matches.sort(key=lambda d: d.host_api != default_api)
    return matches[0].index


def rms_dbfs(samples: np.ndarray) -> float:
    """Loudness of float samples in dBFS (0 = full scale, silence ~ -100)."""
    if samples.size == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return 20 * math.log10(rms) if rms > 1e-5 else -100.0


def peak_dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -100.0
    peak = float(np.max(np.abs(samples)))
    return 20 * math.log10(peak) if peak > 1e-5 else -100.0


def _lowpass_taps(cutoff: float, count: int = 161) -> np.ndarray:
    """Windowed-sinc low-pass FIR; ``cutoff`` is a fraction of the sample rate (0..0.5)."""
    n = np.arange(count) - (count - 1) / 2
    taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(count)
    return (taps / taps.sum()).astype(np.float32)


class StreamResampler:
    """Anti-aliased sample-rate conversion that works chunk by chunk (filter and position carried over).

    The old version averaged blocks or interpolated linearly with no low-pass filter, so everything above the new
    Nyquist (8 kHz for 16 kHz) folded back into the speech band as noise (typical for 44.1 kHz microphones), and
    every chunk was converted separately, leaving a click at each seam. This low-passes first, then interpolates.
    """

    def __init__(self, source_rate: int, target_rate: int = SAMPLE_RATE) -> None:
        self.source_rate, self.target_rate = int(source_rate), int(target_rate)
        self._same = self.source_rate == self.target_rate
        self._step = self.source_rate / self.target_rate
        self._taps = _lowpass_taps(min(0.5, 0.46 * self.target_rate / self.source_rate)) if self.source_rate > self.target_rate else None
        self._history = np.zeros(0, dtype=np.float32)   # last samples of the filtered signal
        self._tail = np.zeros(0, dtype=np.float32)      # raw samples the filter still needs
        self._pos = 0.0                                  # position of the next output sample inside ``_history``

    def process(self, samples: np.ndarray) -> np.ndarray:
        samples = np.asarray(samples, dtype=np.float32).ravel()
        if self._same or samples.size == 0:
            return samples
        if self._taps is not None:
            keep = self._taps.size - 1
            data = np.concatenate((self._tail, samples))
            if data.size <= keep:
                self._tail = data
                return np.zeros(0, dtype=np.float32)
            filtered = np.convolve(data, self._taps, mode="valid").astype(np.float32)
            self._tail = data[-keep:]
        else:
            filtered = samples                           # upsampling: nothing to remove first
        signal = np.concatenate((self._history, filtered))
        count = int(np.floor((signal.size - 1 - self._pos) / self._step)) + 1
        if count <= 0:
            self._history = signal
            return np.zeros(0, dtype=np.float32)
        positions = self._pos + self._step * np.arange(count)
        out = np.interp(positions, np.arange(signal.size), signal).astype(np.float32)
        next_pos = self._pos + self._step * count
        drop = int(np.floor(next_pos))
        self._history = signal[drop:]
        self._pos = next_pos - drop
        return out


def resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Convert a whole clip to ``target_rate`` (anti-aliased)."""
    samples = np.asarray(samples, dtype=np.float32)
    if source_rate == target_rate or samples.size == 0:
        return samples
    r = StreamResampler(source_rate, target_rate)
    pad = np.zeros(r._taps.size // 2 if r._taps is not None else 0, dtype=np.float32)   # keep timing and length exact
    out = r.process(np.concatenate((pad, samples, pad)))
    want = int(round(samples.size * target_rate / source_rate))
    return out[:want] if out.size >= want else np.pad(out, (0, want - out.size))


def remove_rumble(samples: np.ndarray, cutoff_hz: float = 80.0, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Remove DC offset and low-frequency rumble (desk thumps, fans, AC hum) that carry no speech.

    A 2nd-order Butterworth high-pass applied forwards and backwards (zero phase), so speech timing is untouched.
    Speech starts around 85 Hz (male fundamental), so 80 Hz is safe.
    """
    x = np.asarray(samples, dtype=np.float32)
    if x.size < 64:
        return x
    k = np.tan(np.pi * cutoff_hz / rate)
    q = np.sqrt(2.0)
    norm = 1.0 / (1.0 + q * k + k * k)
    b = np.array([norm, -2 * norm, norm])
    a = np.array([1.0, 2 * (k * k - 1) * norm, (1.0 - q * k + k * k) * norm])

    def run(v: np.ndarray) -> np.ndarray:
        out = np.empty_like(v, dtype=np.float64)
        z1 = z2 = 0.0
        b0, b1, b2, a1, a2 = b[0], b[1], b[2], a[1], a[2]
        for i, xi in enumerate(v.tolist()):
            y = b0 * xi + z1
            z1 = b1 * xi - a1 * y + z2
            z2 = b2 * xi - a2 * y
            out[i] = y
        return out

    centred = x.astype(np.float64) - float(np.mean(x))
    return run(run(centred)[::-1])[::-1].astype(np.float32)
