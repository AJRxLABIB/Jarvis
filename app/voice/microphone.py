"""Microphone recording.

The stream is open only while recording (the "microphone active" indicator is
driven by ``on_active_change``) and is always closed again, including on errors.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from app.utils.logging_setup import get_logger, log_event
from app.voice.audio import (
    SAMPLE_RATE,
    AudioDeviceError,
    AudioError,
    import_sounddevice,
    peak_dbfs,
    resample,
    resolve_device,
    rms_dbfs,
)
from app.voice.vad import EnergyVAD

_log = get_logger("microphone")
FRAME_SECONDS = 0.03
PREROLL_SECONDS = 0.3
TRAILING_KEEP_SECONDS = 0.25

StopReason = Literal["silence", "max_length", "stopped", "no_speech"]


class MicrophoneDisabledError(AudioError):
    pass


@dataclass(frozen=True)
class Recording:
    audio: np.ndarray  # float32 mono at SAMPLE_RATE
    reason: StopReason
    speech_detected: bool
    peak_db: float

    @property
    def duration(self) -> float:
        return self.audio.size / SAMPLE_RATE


@dataclass(frozen=True)
class LevelReport:
    seconds: float
    peak_db: float
    average_db: float
    clipped: bool

    @property
    def verdict(self) -> str:
        if self.peak_db < -50:
            return "No sound detected. Check that the right microphone is selected and not muted."
        if self.peak_db < -30:
            return "Very quiet. Move closer or raise the microphone level in Windows sound settings."
        if self.clipped:
            return "Too loud (clipping). Lower the microphone level in Windows sound settings."
        return "Microphone level looks good."


class Microphone:
    def __init__(
        self,
        device: str = "",
        *,
        enabled: bool = True,
        on_active_change: Callable[[bool], None] | None = None,
        on_level: Callable[[float], None] | None = None,
        sd: Any = None,
    ) -> None:
        self.device = device
        self.enabled = enabled
        self.on_active_change = on_active_change
        self.on_level = on_level
        self._sd = sd
        self._active = False
        self._lock = threading.Lock()  # one recording at a time

    @property
    def active(self) -> bool:
        return self._active

    def _set_active(self, value: bool) -> None:
        self._active = value
        if self.on_active_change is not None:
            try:
                self.on_active_change(value)
            except Exception:
                _log.warning("microphone indicator callback failed", exc_info=True)

    def _open_stream(self, sd: Any, device: int | None, frames: "queue.Queue[np.ndarray]") -> tuple[Any, int]:
        def callback(indata: np.ndarray, _frames: int, _time: Any, status: Any) -> None:
            if status:
                _log.debug("input stream status: %s", status)
            try:
                frames.put_nowait(indata[:, 0].copy())
            except queue.Full:  # the reader fell behind (e.g. while transcribing): drop the oldest audio
                try:
                    frames.get_nowait()
                    frames.put_nowait(indata[:, 0].copy())
                except (queue.Empty, queue.Full):
                    pass

        rates = [SAMPLE_RATE]
        try:
            info = sd.query_devices(device, "input")
            rates.append(int(info["default_samplerate"]))
        except Exception:
            pass
        last_error: Exception | None = None
        for rate in dict.fromkeys(rates):
            try:
                stream = sd.InputStream(
                    device=device, channels=1, samplerate=rate, dtype="float32",
                    blocksize=int(rate * FRAME_SECONDS), callback=callback,
                )
                stream.start()
                return stream, rate
            except Exception as exc:  # PortAudioError: rate unsupported, device busy ...
                last_error = exc
        raise AudioDeviceError(f"Could not open the microphone: {last_error}")

    def record(
        self,
        *,
        silence_timeout: float = 0.9,
        start_timeout: float = 6.0,
        max_seconds: float = 15.0,
        stop_event: threading.Event | None = None,
        vad=None,  # EnergyVAD or SileroVAD
        fixed_seconds: float | None = None,
    ) -> Recording:
        """Record one utterance: wait for speech, stop after ``silence_timeout``
        seconds of silence. With ``fixed_seconds`` simply record that long."""
        if not self.enabled:
            raise MicrophoneDisabledError("The microphone is turned off in Settings.")
        if not self._lock.acquire(blocking=False):
            raise AudioError("The microphone is already recording.")
        sd = self._sd or import_sounddevice()
        vad = vad or EnergyVAD()
        frames: queue.Queue[np.ndarray] = queue.Queue()
        stream = None
        try:
            device = resolve_device(self.device, "input", sd)
            stream, rate = self._open_stream(sd, device, frames)
            self._set_active(True)
            log_event(_log, "microphone_opened", device=device, samplerate=rate)

            preroll: deque[np.ndarray] = deque(maxlen=max(1, int(PREROLL_SECONDS / FRAME_SECONDS)))
            captured: list[np.ndarray] = []
            speech_started = False
            # Timing uses the amount of audio received (deterministic); the wall
            # clock only guards against a device that stops delivering audio.
            audio_time = 0.0
            last_speech = 0.0
            limit = fixed_seconds if fixed_seconds is not None else max_seconds
            wall_deadline = time.monotonic() + limit + 3.0
            reason: StopReason = "no_speech"
            peak = -100.0

            while True:
                if stop_event is not None and stop_event.is_set():
                    reason = "stopped" if speech_started or fixed_seconds else "no_speech"
                    break
                if fixed_seconds is not None and audio_time >= fixed_seconds:
                    reason = "max_length"
                    break
                if fixed_seconds is None:
                    if speech_started and audio_time - last_speech >= silence_timeout:
                        reason = "silence"
                        break
                    if not speech_started and audio_time >= start_timeout:
                        break
                    if audio_time >= max_seconds:
                        reason = "max_length" if speech_started else "no_speech"
                        break
                if time.monotonic() > wall_deadline:
                    raise AudioDeviceError("The microphone stopped sending audio.")
                try:
                    frame = frames.get(timeout=0.1)
                except queue.Empty:
                    continue
                audio_time += frame.size / rate
                peak = max(peak, peak_dbfs(frame))
                if self.on_level is not None:
                    self.on_level(rms_dbfs(frame))
                if fixed_seconds is not None:
                    captured.append(frame)
                    continue
                if vad.is_speech(frame):
                    if not speech_started:
                        speech_started = True
                        captured.extend(preroll)
                    last_speech = audio_time
                if speech_started:
                    captured.append(frame)
                else:
                    preroll.append(frame)
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    _log.warning("closing the microphone stream failed", exc_info=True)
                self._set_active(False)
                log_event(_log, "microphone_closed")
            self._lock.release()

        audio = np.concatenate(captured) if captured else np.zeros(0, dtype=np.float32)
        audio = resample(audio, rate, SAMPLE_RATE)
        if reason == "silence":  # drop most of the trailing silence
            keep = audio.size - int((silence_timeout - TRAILING_KEEP_SECONDS) * SAMPLE_RATE)
            audio = audio[: max(keep, 0)] if keep > SAMPLE_RATE * 0.2 else audio
        speech = speech_started or (fixed_seconds is not None and peak > -50)
        return Recording(audio=audio.astype(np.float32), reason=reason, speech_detected=speech, peak_db=peak)

    def open_stream(self, frame_samples: int = 1280) -> "MicrophoneStream":
        """A continuous stream of fixed-size 16 kHz frames (hands-free listening).

        Holds the recording lock until closed, so push-to-talk can't open a
        second stream at the same time. The caller reports what the audio is
        used for (waiting for the wake word vs. recording a command).
        """
        if not self.enabled:
            raise MicrophoneDisabledError("The microphone is turned off in Settings.")
        if not self._lock.acquire(blocking=False):
            raise AudioError("The microphone is already recording.")
        try:
            sd = self._sd or import_sounddevice()
            frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)  # ~12 s at 30 ms blocks
            device = resolve_device(self.device, "input", sd)
            stream, rate = self._open_stream(sd, device, frames)
        except BaseException:
            self._lock.release()
            raise
        log_event(_log, "microphone_stream_opened", device=device, samplerate=rate)
        return MicrophoneStream(self, stream, frames, rate, frame_samples)

    def level_test(self, seconds: float = 3.0) -> tuple[LevelReport, Recording]:
        recording = self.record(fixed_seconds=seconds)
        levels = [rms_dbfs(chunk) for chunk in np.array_split(recording.audio, max(1, int(seconds * 10)))]
        clipped = bool(recording.audio.size and np.max(np.abs(recording.audio)) >= 0.99)
        report = LevelReport(
            seconds=recording.duration, peak_db=recording.peak_db,
            average_db=float(np.mean(levels)) if levels else -100.0, clipped=clipped,
        )
        return report, recording


class MicrophoneStream:
    """Fixed-size float32 frames at 16 kHz from an open input stream. Always close it."""

    def __init__(self, microphone: Microphone, stream: Any, frames: "queue.Queue[np.ndarray]", rate: int,
                 frame_samples: int) -> None:
        self._microphone = microphone
        self._stream = stream
        self._frames = frames
        self.rate = rate
        self.frame_samples = frame_samples
        self._pending = np.zeros(0, dtype=np.float32)
        self._closed = False
        self.last_audio = time.monotonic()

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        """The next frame, or None if no audio arrived within ``timeout``."""
        deadline = time.monotonic() + timeout
        while self._pending.size < self.frame_samples:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                chunk = self._frames.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            self.last_audio = time.monotonic()
            self._pending = np.concatenate((self._pending, resample(chunk, self.rate, SAMPLE_RATE)))
        frame, self._pending = self._pending[: self.frame_samples], self._pending[self.frame_samples:]
        return frame.astype(np.float32)

    def discard(self) -> None:
        """Drop audio that queued up (e.g. while speech was being transcribed or the assistant spoke)."""
        self._pending = np.zeros(0, dtype=np.float32)
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            _log.warning("closing the microphone stream failed", exc_info=True)
        finally:
            self._microphone._lock.release()
            log_event(_log, "microphone_stream_closed")

    def __enter__(self) -> "MicrophoneStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
