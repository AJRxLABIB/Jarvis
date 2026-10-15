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


def resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Mono resampling. Integer down-sampling uses block averaging (cheap anti-aliasing)."""
    samples = np.asarray(samples, dtype=np.float32)
    if source_rate == target_rate or samples.size == 0:
        return samples
    if source_rate > target_rate and source_rate % target_rate == 0:
        factor = source_rate // target_rate
        usable = samples.size - samples.size % factor
        return samples[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)
    duration = samples.size / source_rate
    target_len = max(1, int(round(duration * target_rate)))
    positions = np.linspace(0, samples.size - 1, target_len)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)
