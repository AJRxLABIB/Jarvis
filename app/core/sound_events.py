"""Environmental sound events: short audio windows -> a label and a score -> a notification, with duplicates suppressed. Local, opt-in, nothing recorded.

* A monitoring SESSION is explicit (``start``), shows a state the interface can display, stops with ``stop`` and ends by itself after ``max_minutes``.
* Audio windows are scored and thrown away: only the label, score and time are kept (bounded history). Nothing is saved or uploaded.
* ``LoudSoundClassifier`` is the always-available, CPU-only fallback: it can only say that a sound was much louder than the room's usual level ("loud_sound").
  It does NOT know a knock from a slammed door. Real labels (doorbell, knock, glass breaking...) need a sound-classification model: ``YamnetOnnxClassifier`` runs a
  YAMNet ONNX model the user placed on disk (nothing is downloaded automatically; unverified here - no model, no onnxruntime in the test environment).
* A rule fires only when the score stays at or above its threshold for ``consecutive`` windows, and then not again within ``cooldown_s``.

This is a convenience, not a safety system: it misses sounds and mislabels others. It must not be relied on for emergencies, and the interface says so.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

SAMPLE_RATE = 16_000
MAX_SESSION_MIN = 240


def rms_dbfs(samples: Sequence[float]) -> float:
    """Level of a float window in dBFS (full scale = 1.0). Silence -> -120."""
    if not len(samples):
        return -120.0
    mean_sq = sum(float(x) * float(x) for x in samples) / len(samples)
    return -120.0 if mean_sq <= 1e-12 else max(-120.0, 10 * math.log10(mean_sq))


class Classifier(Protocol):
    def classify(self, samples: Sequence[float], sample_rate: int) -> list[tuple[str, float]]: ...


class LoudSoundClassifier:
    """Compares each window with the running quiet level of the room. Score 0..1 = how far above it (full score at +25 dB)."""

    def __init__(self, min_rise_db: float = 12.0, floor_db: float = -55.0, history: int = 30) -> None:
        self.min_rise_db, self.floor_db = min_rise_db, floor_db
        self._levels: deque[float] = deque(maxlen=history)

    def classify(self, samples: Sequence[float], sample_rate: int = SAMPLE_RATE) -> list[tuple[str, float]]:
        level = rms_dbfs(samples)
        baseline = sorted(self._levels)[len(self._levels) // 2] if len(self._levels) >= 5 else None      # median of recent windows
        out = []
        if baseline is not None and level > self.floor_db and level - baseline >= self.min_rise_db:
            out = [("loud_sound", min(1.0, (level - baseline) / 25.0))]
        if baseline is None or not out:
            self._levels.append(level)                                    # a loud window does not raise the "usual" level
        return out


class YamnetOnnxClassifier:
    """YAMNet via onnxruntime. ``session_factory`` and ``labels`` are injected so it can be tested; ``from_files`` loads a model the user placed on disk."""

    def __init__(self, session: Any, labels: list[str], wanted: set[str] | None = None) -> None:
        self.session, self.labels, self.wanted = session, labels, wanted

    @classmethod
    def from_files(cls, model_path: str, labels_csv: str, wanted: set[str] | None = None) -> "YamnetOnnxClassifier":
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("onnxruntime is not installed (pip install onnxruntime); it is optional") from exc
        import csv

        with open(labels_csv, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))[1:]
        labels = [r[2] for r in rows if len(r) >= 3]
        return cls(ort.InferenceSession(model_path, providers=["CPUExecutionProvider"]), labels, wanted)

    def classify(self, samples: Sequence[float], sample_rate: int = SAMPLE_RATE) -> list[tuple[str, float]]:
        import numpy as np

        if sample_rate != SAMPLE_RATE:
            raise ValueError("YAMNet needs 16 kHz audio")
        name = self.session.get_inputs()[0].name
        scores = self.session.run(None, {name: np.asarray(samples, dtype=np.float32)})[0]
        best = np.asarray(scores).max(axis=0) if getattr(scores, "ndim", 1) > 1 else np.asarray(scores)
        out = [(self.labels[i], float(best[i])) for i in range(min(len(self.labels), len(best))) if self.wanted is None or self.labels[i] in self.wanted]
        return sorted(out, key=lambda x: -x[1])[:5]


@dataclass
class SoundRule:
    label: str
    min_score: float = 0.6
    consecutive: int = 2
    cooldown_s: float = 30.0

    def __post_init__(self) -> None:
        if not self.label or not (0.05 <= self.min_score <= 1.0) or not (1 <= self.consecutive <= 10) or not (1 <= self.cooldown_s <= 3600):
            raise ValueError("a sound rule needs a label, a score of 0.05-1, 1-10 windows and a cooldown of 1-3600 s")


@dataclass(frozen=True)
class Detection:
    label: str
    score: float
    t: float


@dataclass
class Session:
    rules: list[SoundRule]
    classifier: Classifier
    started: float
    max_minutes: int = 60
    state: str = "listening"                       # listening | stopped | expired
    detections: deque = field(default_factory=lambda: deque(maxlen=50))
    windows: int = 0
    _streak: dict[str, int] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)

    @property
    def indicator(self) -> str:
        return {"listening": "Listening for: " + ", ".join(r.label for r in self.rules), "stopped": "Sound monitoring is off", "expired": "Sound monitoring ended (time limit)"}[self.state]


class Monitor:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.session: Session | None = None

    def start(self, rules: list[SoundRule], classifier: Classifier, max_minutes: int = 60) -> Session:
        if not rules:
            raise ValueError("choose at least one sound to listen for")
        self.session = Session(list(rules), classifier, self.clock(), max(1, min(int(max_minutes), MAX_SESSION_MIN)))
        return self.session

    def stop(self) -> None:
        if self.session and self.session.state == "listening":
            self.session.state = "stopped"

    def feed(self, samples: Sequence[float], sample_rate: int = SAMPLE_RATE) -> list[Detection]:
        s = self.session
        if s is None or s.state != "listening":
            return []
        now = self.clock()
        if now - s.started > s.max_minutes * 60:
            s.state = "expired"
            return []
        s.windows += 1
        scores = dict(s.classifier.classify(samples, sample_rate))
        out = []
        for rule in s.rules:
            score = scores.get(rule.label, 0.0)
            if score >= rule.min_score:
                s._streak[rule.label] = s._streak.get(rule.label, 0) + 1
            else:
                s._streak[rule.label] = 0
                continue
            if s._streak[rule.label] >= rule.consecutive and now - s._last.get(rule.label, -1e18) >= rule.cooldown_s:
                s._last[rule.label] = now
                s._streak[rule.label] = 0
                d = Detection(rule.label, round(score, 2), now)
                s.detections.append(d)
                out.append(d)
        return out


def describe(m: Monitor) -> str:
    s = m.session
    if s is None:
        return "Sound monitoring has not been started."
    last = f" Last detection: {s.detections[-1].label} ({s.detections[-1].score:.2f}, {int(m.clock() - s.detections[-1].t)} s ago)." if s.detections else " Nothing detected yet."
    return f"{s.indicator}. {s.windows} window(s) checked.{last} (Not an emergency system: it can miss or mislabel sounds.)"


def capture_loop(monitor: Monitor, read_window: Callable[[], Sequence[float] | None], on_detect: Callable[[Detection], None], stop: Any, window_s: float = 1.0) -> None:
    """Runs until ``stop`` (a threading.Event) is set or the session ends. ``read_window`` returns one window of 16 kHz float samples (None = the microphone is gone).
    Windows are scored and dropped; nothing is stored."""
    while not stop.is_set() and monitor.session is not None and monitor.session.state == "listening":
        samples = read_window()
        if samples is None:
            monitor.stop()
            return
        for detection in monitor.feed(samples):
            on_detect(detection)


def sounddevice_reader(window_s: float = 1.0) -> tuple[Callable[[], Sequence[float] | None], Callable[[], None]]:
    """(read, close) for the default microphone via the existing audio helper. Raises if there is no audio support (it is optional)."""
    from app.voice.audio import SAMPLE_RATE as RATE
    from app.voice.audio import import_sounddevice

    sd = import_sounddevice()
    stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32")
    stream.start()
    frames = int(RATE * window_s)

    def read() -> Sequence[float] | None:
        try:
            data, _overflow = stream.read(frames)
            return data[:, 0].tolist()
        except Exception:
            return None

    def close() -> None:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    return read, close
