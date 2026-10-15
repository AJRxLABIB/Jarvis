"""Energy-based voice activity detection for end-of-utterance detection.

Cheap enough to run on every 30 ms frame. The noise floor is learned from the
first frames and keeps adapting while nobody speaks. (Speech recognition runs a
second, neural VAD pass on the finished recording.)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.voice.audio import rms_dbfs


@dataclass(frozen=True)
class VADConfig:
    margin_db: float = 12.0  # speech must be this much louder than the noise floor
    min_threshold_db: float = -50.0
    max_threshold_db: float = -38.0  # normal speech is about -30 dBFS: never set the bar above it
    calibration_frames: int = 8  # ~240 ms
    adapt_rate: float = 0.05


class EnergyVAD:
    def __init__(self, config: VADConfig | None = None) -> None:
        self.config = config or VADConfig()
        self._calibration: list[float] = []
        self.noise_db: float | None = None

    @property
    def threshold_db(self) -> float:
        c = self.config
        noise = self.noise_db if self.noise_db is not None else c.min_threshold_db - c.margin_db
        return min(max(noise + c.margin_db, c.min_threshold_db), c.max_threshold_db)

    def is_speech(self, frame: np.ndarray) -> bool:
        level = rms_dbfs(frame)
        c = self.config
        if self.noise_db is None:
            self._calibration.append(level)
            if len(self._calibration) >= c.calibration_frames:
                # low percentile: robust if the user starts talking immediately
                self.noise_db = float(np.percentile(self._calibration, 20))
            return level > c.max_threshold_db  # only clearly loud speech counts while calibrating
        speech = level > self.threshold_db
        if not speech:
            self.noise_db += c.adapt_rate * (level - self.noise_db)
        return speech

    def reset(self) -> None:
        self._calibration.clear()
        self.noise_db = None


# ------------------------------------------------------------------ neural VAD


def silero_model_path() -> Path | None:
    """The Silero VAD model faster-whisper ships (no extra download)."""
    try:
        import faster_whisper
    except Exception:
        return None
    assets = Path(faster_whisper.__file__).parent / "assets"
    for name in ("silero_vad_v6.onnx", "silero_vad.onnx"):
        if (assets / name).is_file():
            return assets / name
    return None


class SileroVAD:
    """Neural speech detection (Silero VAD, ONNX, ~0.1 ms per 32 ms chunk on the CPU).

    Unlike the energy detector it isn't fooled by fans, typing, music or a TV in the
    background, and it keeps quiet speech as speech, so utterances end when the user
    stops talking, not when the room gets quieter. Same interface as ``EnergyVAD``.
    """

    CHUNK = 512  # samples at 16 kHz
    CONTEXT = 64

    def __init__(self, model_path=None, *, start: float = 0.5, keep: float = 0.35, floor_db: float = -62.0,
                 session=None) -> None:
        self.start, self.keep, self.floor_db = start, keep, floor_db
        if session is None:
            import onnxruntime

            path = model_path or silero_model_path()
            if path is None:
                raise RuntimeError("the Silero VAD model was not found")
            opts = onnxruntime.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.log_severity_level = 4
            session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=opts)
        self._session = session
        self.last_probability = 0.0
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._speaking = False

    def probability(self, frame: np.ndarray) -> float:
        audio = np.concatenate([self._pending, np.asarray(frame, dtype=np.float32).ravel()])
        best = None
        while audio.size >= self.CHUNK:
            chunk, audio = audio[: self.CHUNK], audio[self.CHUNK:]
            window = np.concatenate([self._context, chunk])[None, :]
            out, self._h, self._c = self._session.run(None, {"input": window, "h": self._h, "c": self._c})
            self._context = chunk[-self.CONTEXT:]
            p = float(np.ravel(out)[0])
            best = p if best is None else max(best, p)
        self._pending = audio
        if best is not None:
            self.last_probability = best
        return self.last_probability

    def is_speech(self, frame: np.ndarray) -> bool:
        p = self.probability(frame)
        loud_enough = rms_dbfs(frame) > self.floor_db
        self._speaking = loud_enough and p >= (self.keep if self._speaking else self.start)
        return self._speaking


def create_vad(engine: str = "auto"):
    """``auto``/``silero``: the neural detector when available, else energy."""
    if engine in ("auto", "silero"):
        try:
            return SileroVAD()
        except Exception:
            from app.utils.logging_setup import get_logger

            get_logger("vad").warning("Silero VAD unavailable; using the energy detector", exc_info=engine == "silero")
    return EnergyVAD()
