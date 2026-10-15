from __future__ import annotations

import threading

import numpy as np
import pytest

from app.voice.audio import AudioDeviceError, list_devices, peak_dbfs, resample, resolve_device, rms_dbfs
from app.voice.microphone import Microphone, MicrophoneDisabledError
from app.voice.vad import EnergyVAD
from tests.fake_audio import FakeSoundDevice, noise, tone


def test_levels() -> None:
    assert rms_dbfs(np.zeros(100, dtype=np.float32)) == -100.0
    assert -4 < rms_dbfs(np.ones(100, dtype=np.float32) * 0.7) < -2
    assert peak_dbfs(np.array([0.5, -1.0], dtype=np.float32)) == pytest.approx(0.0)


def test_resample() -> None:
    x = tone(1.0, rate=48000)
    assert resample(x, 48000, 16000).size == 16000
    assert resample(x, 48000, 22050).size == 22050
    assert resample(x, 16000, 16000) is not None


def test_device_listing_and_resolution() -> None:
    sd = FakeSoundDevice()
    inputs = list_devices("input", sd)
    assert [d.index for d in inputs] == [0, 1, 2] and inputs[1].is_default_input
    assert resolve_device("", "input", sd) is None
    assert resolve_device("2", "input", sd) == 2
    assert resolve_device("usb", "input", sd) == 1  # MME (default host API) preferred over WASAPI
    assert resolve_device("realtek", "output", sd) == 3
    with pytest.raises(AudioDeviceError):
        resolve_device("3", "input", sd)  # a speaker, not a microphone
    with pytest.raises(AudioDeviceError, match="--devices"):
        resolve_device("blue yeti", "input", sd)


def test_vad_detects_speech_over_noise() -> None:
    vad = EnergyVAD()
    frames = np.split(noise(0.3)[:4800], 10)
    assert not any(vad.is_speech(f) for f in frames)
    assert vad.is_speech(tone(0.03, 0.1))
    assert not vad.is_speech(noise(0.03, seed=5))


def _mic(audio: np.ndarray, **kwargs) -> tuple[Microphone, FakeSoundDevice, list[bool]]:
    sd = FakeSoundDevice(audio, **kwargs)
    states: list[bool] = []
    return Microphone(sd=sd, on_active_change=states.append), sd, states


def test_records_until_silence_and_releases_device() -> None:
    audio = np.concatenate([noise(0.5), tone(1.0), noise(3.0)])
    mic, sd, states = _mic(audio)
    rec = mic.record(silence_timeout=0.5, start_timeout=3, max_seconds=10)
    assert rec.reason == "silence" and rec.speech_detected
    assert 0.9 < rec.duration < 2.0  # speech + pre-roll + a little trailing silence
    assert states == [True, False] and sd.open_streams == 0 and not mic.active


def test_no_speech_times_out() -> None:
    mic, _, _ = _mic(noise(5.0))
    rec = mic.record(silence_timeout=0.5, start_timeout=0.5, max_seconds=10)
    assert rec.reason == "no_speech" and not rec.speech_detected


def test_stop_event_and_max_length() -> None:
    mic, _, _ = _mic(tone(30.0), realtime=0.001)
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    assert mic.record(stop_event=stop, max_seconds=20).reason == "stopped"
    mic, _, _ = _mic(tone(30.0))
    assert mic.record(silence_timeout=5, max_seconds=2.0).reason == "max_length"


def test_falls_back_to_device_rate_and_resamples() -> None:
    audio = np.concatenate([noise(0.3), tone(1.0), noise(2.0)])
    mic, sd, _ = _mic(audio, supported_rates=(48000,))
    rec = mic.record(silence_timeout=0.5)
    assert sd.opened_inputs[-1]["samplerate"] == 48000 and rec.speech_detected
    assert 0.9 < rec.duration < 2.0


def test_disabled_microphone() -> None:
    with pytest.raises(MicrophoneDisabledError):
        Microphone(enabled=False, sd=FakeSoundDevice()).record()


def test_level_test_verdicts() -> None:
    mic, _, _ = _mic(tone(2.0, 0.3))
    report, _ = mic.level_test(1.0)
    assert report.verdict == "Microphone level looks good."
    mic, _, _ = _mic(np.zeros(32000, dtype=np.float32))
    assert "No sound" in mic.level_test(0.5)[0].verdict
