"""Presence awareness (opt-in, PRESENCE_DETECTION): notices when you sit down or walk away.

Every couple of seconds one webcam frame is checked for a face (OpenCV's built-in face
detector, on this PC). Frames are never saved, shown or sent anywhere; only "a face is
there / isn't" is used. While it runs, the app shows CAMERA ACTIVE.

* Away for PRESENCE_AWAY_MINUTES -> the PC is locked (PRESENCE_LOCK).
* Back after being away -> "Welcome back" (and a greeting in the chat).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("presence")
CHECK_SECONDS = 2.0
RETURN_FRAMES = 2  # faces in this many checks in a row = you're back


class FaceDetector:
    """OpenCV Haar-cascade face detection (bundled with opencv-python-headless)."""

    def __init__(self) -> None:
        import cv2

        self._cv2 = cv2
        self._cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if self._cascade.empty():
            raise RuntimeError("the face detector model is missing")

    def has_face(self, frame) -> bool:
        cv2 = self._cv2
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        gray = cv2.equalizeHist(cv2.resize(gray, (320, int(320 * gray.shape[0] / max(1, gray.shape[1])))))
        faces = self._cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(40, 40))
        return len(faces) > 0


class Camera:
    def __init__(self, index: int = 0) -> None:
        import cv2

        self._cv2 = cv2
        backend = getattr(cv2, "CAP_DSHOW", 0)  # DirectShow opens faster on Windows
        self._capture = cv2.VideoCapture(index, backend)
        if not self._capture.isOpened():
            raise RuntimeError("no webcam could be opened")
        # Ask for 640x480 MJPG at 30 fps: many webcams default to slow uncompressed YUY2 (about 15 fps). Drivers may ignore any of these.
        for prop, value in ((getattr(cv2, "CAP_PROP_FOURCC", None), cv2.VideoWriter_fourcc(*"MJPG")), (getattr(cv2, "CAP_PROP_FRAME_WIDTH", None), 640),
                            (getattr(cv2, "CAP_PROP_FRAME_HEIGHT", None), 480), (getattr(cv2, "CAP_PROP_FPS", None), 30),
                            (getattr(cv2, "CAP_PROP_BUFFERSIZE", None), 1)):
            try:
                if prop is not None:
                    self._capture.set(prop, value)
            except Exception:
                pass

    def frame(self):
        ok, frame = self._capture.read()
        return frame if ok else None

    def close(self) -> None:
        self._capture.release()


def _shared_lease():
    """Presence shares the webcam with gesture control through the broker (one device, ref-counted)."""
    from app.gesture.camera import shared_broker

    return shared_broker().lease()


class PresenceMonitor:
    def __init__(self, *, away_minutes: float, on_away: Callable[[], None], on_return: Callable[[float], None],
                 on_camera: Callable[[bool], None] | None = None, camera_factory=None, detector_factory=FaceDetector,
                 clock: Callable[[], float] = time.monotonic, check_seconds: float = CHECK_SECONDS) -> None:
        self.away_seconds = away_minutes * 60
        self.on_away, self.on_return, self.on_camera = on_away, on_return, on_camera
        self._camera_factory, self._detector_factory = camera_factory or _shared_lease, detector_factory
        self._clock = clock
        self.check_seconds = check_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.present = True
        self._last_seen = clock()
        self._away_since: float | None = None
        self._seen_in_row = 0
        self.problem: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="presence", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def observe(self, face: bool) -> str | None:
        """One check. Returns "away" / "back" when that changes (also used by the tests)."""
        now = self._clock()
        if face:
            self._seen_in_row += 1
            self._last_seen = now
            if not self.present and self._seen_in_row >= RETURN_FRAMES:
                gone = now - (self._away_since if self._away_since is not None else now)
                self.present, self._away_since = True, None
                log_event(_log, "user_returned", away_minutes=round(gone / 60, 1))
                self.on_return(gone)
                return "back"
            return None
        self._seen_in_row = 0
        if self.present and now - self._last_seen >= self.away_seconds:
            self.present, self._away_since = False, self._last_seen
            log_event(_log, "user_away")
            self.on_away()
            return "away"
        return None

    def _run(self) -> None:
        camera = None
        try:
            detector = self._detector_factory()
            camera = self._camera_factory()
            if self.on_camera:
                self.on_camera(True)
            while not self._stop.wait(self.check_seconds):
                frame = camera.frame()
                if frame is not None:
                    self.observe(detector.has_face(frame))
        except Exception as exc:
            self.problem = f"Presence detection stopped: {exc}"
            _log.warning("presence detection failed", exc_info=True)
        finally:
            if camera is not None:
                camera.close()
            if self.on_camera:
                self.on_camera(False)
