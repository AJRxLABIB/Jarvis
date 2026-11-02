"""Hand detector: frame in, Hand (21 landmarks) out. MediaPipe Hand Landmarker (Apache-2.0, runs locally on the CPU) behind a small interface.

* The model file (hand_landmarker.task, ~7.5 MB) is NOT bundled and is NEVER downloaded silently. `python run.py --setup-hand-model`
  (or scripts/setup_hand_model.py) fetches it once, on your explicit request, and verifies it. Detection itself never uses the network.
* mediapipe is an optional dependency (`pip install mediapipe`); `availability()` says exactly what is missing."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.gesture.landmarks import Hand

MODEL_NAME = "hand_landmarker.task"
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
MIN_MODEL_BYTES = 1_000_000


class HandDetector(Protocol):
    def detect(self, frame, timestamp_ms: int) -> Hand | None: ...
    def close(self) -> None: ...


def model_path(models_dir: Path) -> Path:
    return Path(models_dir) / MODEL_NAME


def availability(models_dir: Path) -> tuple[bool, str]:
    """(ready, what is missing) without importing anything heavy."""
    import importlib.util

    problems = []
    if importlib.util.find_spec("mediapipe") is None:
        problems.append("the mediapipe package is not installed (run: pip install -r requirements-gesture.txt)")
    path = model_path(models_dir)
    if not path.is_file() or path.stat().st_size < MIN_MODEL_BYTES:
        problems.append(f"the hand model is not set up yet (run: python run.py --setup-hand-model; it saves {MODEL_NAME} once, on request)")
    return (not problems, "; ".join(problems))


def hands_from_result(result) -> list[Hand]:
    """Every hand in a MediaPipe result (up to two), in the order MediaPipe lists them."""
    return [h for h in (hand_from_result(result, i) for i in range(len(getattr(result, "hand_landmarks", None) or []))) if h is not None]


def hand_from_result(result, max_hands_index: int = 0) -> Hand | None:
    """Convert a MediaPipe HandLandmarkerResult to our Hand (pure; also used by tests with a fake result)."""
    if not getattr(result, "hand_landmarks", None):
        return None
    i = max_hands_index
    pts = tuple((float(p.x), float(p.y), float(getattr(p, "z", 0.0))) for p in result.hand_landmarks[i])
    side, score = "unknown", 1.0
    cats = (getattr(result, "handedness", None) or [[]])[i] if getattr(result, "handedness", None) else []
    if cats:
        side, score = str(cats[0].category_name).lower(), float(cats[0].score)
    return Hand(pts, score, side)


class MediaPipeDetector:
    """Real detector (hardware/model-unverified in CI: needs the model file and a camera; checked by `--validate`)."""

    def __init__(self, models_dir: Path, *, max_hands: int = 1, detection_confidence: float = 0.6, presence_confidence: float = 0.5,
                 tracking_confidence: float = 0.5) -> None:
        ok, why = availability(models_dir)
        if not ok:
            raise RuntimeError(f"Hand tracking is not ready: {why}")
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        self._mp = mp
        options = vision.HandLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(model_path(models_dir))), running_mode=vision.RunningMode.VIDEO,
            num_hands=max(1, min(2, max_hands)), min_hand_detection_confidence=detection_confidence,
            min_hand_presence_confidence=presence_confidence, min_tracking_confidence=tracking_confidence)
        self._landmarker = vision.HandLandmarker.create_from_options(options)
        self._last_ts = -1

    def detect(self, frame, timestamp_ms: int) -> Hand | None:
        hands = self.detect_all(frame, timestamp_ms)
        return hands[0] if hands else None

    def detect_all(self, frame, timestamp_ms: int) -> list[Hand]:
        import cv2

        timestamp_ms = max(timestamp_ms, self._last_ts + 1)  # MediaPipe needs strictly increasing timestamps
        self._last_ts = timestamp_ms
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        return hands_from_result(self._landmarker.detect_for_video(image, timestamp_ms))

    def close(self) -> None:
        try:
            self._landmarker.close()
        except Exception:
            pass


def download_model(models_dir: Path, *, opener=None) -> Path:
    """Explicit one-time setup. `opener(url) -> bytes` is injectable for tests."""
    import urllib.request

    target = model_path(models_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = opener(MODEL_URL) if opener else urllib.request.urlopen(MODEL_URL, timeout=60).read()  # noqa: S310 - fixed https URL
    if len(data) < MIN_MODEL_BYTES:
        raise RuntimeError("the downloaded hand model looks incomplete; nothing was saved")
    tmp = target.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return target
