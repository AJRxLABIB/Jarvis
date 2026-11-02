"""Gesture validation: the parts of hand control that only a real PC can prove.

``python run.py --validate gesture`` checks, on this machine: Python/mediapipe wheel compatibility, the hand model file, that the webcam opens
and delivers frames, real MediaPipe detection (you are asked to hold your hand up: detection rate, measured frames/s and detection
milliseconds), DPI awareness, the pointer landing exactly where the cursor mapper says on every monitor (SetCursorPos read back with
GetCursorPos; the mouse moves, nothing is clicked), and which screen area the cursor covers (whole monitor including the taskbar).
The decisions are tested with a fake ``GestureEnv``; whether YOUR hardware passes is only known when you run it. Pending is never a pass."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from app.validation.report import FAIL, PASS, PENDING, SKIP, WARN, Report, Result, timed

MIN_FPS = 15.0
GOOD_DETECT_MS = 50.0
SLOW_DETECT_MS = 120.0
MIN_HAND_RATE = 0.6
POINTER_TOLERANCE_PX = 2


@dataclass
class DetectRun:
    frames: int
    with_hand: int
    seconds: float
    latencies_ms: list[float]


class GestureEnv:
    """What the checks need. ``RealGestureEnv`` uses the real camera, MediaPipe and Win32; tests use a fake."""

    def is_windows(self) -> bool: ...
    def python_version(self) -> tuple[int, int]: ...
    def mediapipe(self) -> tuple[bool, str]: ...                       # (importable, version or reason)
    def model_ready(self) -> tuple[bool, str]: ...
    def camera_frames(self, seconds: float) -> tuple[int, str]: ...    # (frames received, error text); opens and releases the camera
    def detect(self, seconds: float) -> DetectRun: ...                 # asks the user to hold a hand up; real detector
    def dpi_awareness(self) -> str: ...
    def screens(self) -> list[tuple[int, int, int, int]]: ...           # the rectangles the gesture cursor would use (left, top, right, bottom)
    def pointer_roundtrip(self, x: int, y: int) -> tuple[int, int]: ...  # SetCursorPos then GetCursorPos
    def taskbar_reachable(self) -> bool | None: ...


def check_platform(env: GestureEnv) -> tuple[str, str]:
    if not env.is_windows():
        return PENDING, "not Windows: gesture control moves the real mouse with Win32, so it can only be validated on Windows"
    return PASS, "Windows"


def check_python_wheel(env: GestureEnv) -> tuple[str, str]:
    ok, info = env.mediapipe()
    major, minor = env.python_version()
    if ok:
        return PASS, f"mediapipe {info} imports on Python {major}.{minor}"
    return FAIL, (f"mediapipe is not usable on Python {major}.{minor} ({info}). Install it in this environment: pip install -r requirements-gesture.txt "
                  "(mediapipe 1.x ships one Windows wheel for every Python 3, including 3.14). If pip still finds nothing, keep gesture control off")


def check_model(env: GestureEnv) -> tuple[str, str]:
    ok, info = env.model_ready()
    return (PASS, info) if ok else (FAIL, f"{info} - run: python run.py --setup-hand-model (after installing mediapipe)")


def check_camera(env: GestureEnv) -> tuple[str, str, dict[str, Any]]:
    frames, error = env.camera_frames(2.0)
    if frames == 0:
        return FAIL, f"the webcam delivered no frames ({error or 'busy, unplugged, or blocked by Windows camera privacy'})", {"frames": 0}
    fps = frames / 2.0
    return (PASS if fps >= MIN_FPS else WARN), f"{frames} frames in 2 s = {fps:.1f} fps (needs about {MIN_FPS:.0f}+)", {"fps": round(fps, 1)}


def check_detection(run: DetectRun) -> tuple[str, str, dict[str, Any]]:
    if run.frames == 0:
        return FAIL, "the detector processed no frames", {}
    fps = run.frames / max(1e-6, run.seconds)
    rate = run.with_hand / run.frames
    lat = sorted(run.latencies_ms) or [0.0]
    median, p95 = statistics.median(lat), lat[min(len(lat) - 1, int(len(lat) * 0.95))]
    ev = {"fps": round(fps, 1), "hand_rate": round(rate, 2), "median_ms": round(median, 1), "p95_ms": round(p95, 1), "frames": run.frames}
    detail = f"{run.frames} frames, hand seen in {rate:.0%}, {fps:.1f} fps, detection median {median:.0f} ms / p95 {p95:.0f} ms"
    if rate < MIN_HAND_RATE:
        return WARN, detail + " - the hand was seen in too few frames (was your hand in view and well lit?)", ev
    if median > SLOW_DETECT_MS:
        return FAIL, detail + f" - too slow for smooth control (> {SLOW_DETECT_MS:.0f} ms)", ev
    if median > GOOD_DETECT_MS or fps < MIN_FPS:
        return WARN, detail + " - usable but laggy; lower the camera fps or close other apps", ev
    return PASS, detail, ev


def check_dpi(env: GestureEnv) -> tuple[str, str]:
    mode = env.dpi_awareness()
    if mode == "per-monitor":
        return PASS, "per-monitor DPI aware: cursor pixels are physical pixels"
    if mode == "unknown":
        return WARN, "could not read the DPI awareness"
    return FAIL, f"DPI awareness is '{mode}': at a scaling other than 100% the cursor would land in the wrong place"


def check_pointer(env: GestureEnv) -> tuple[str, str, dict[str, Any]]:
    bad, tested = [], 0
    for n, (x0, y0, x1, y1) in enumerate(env.screens(), start=1):
        for label, x, y in (("top-left", x0, y0), ("centre", (x0 + x1) // 2, (y0 + y1) // 2), ("bottom-right", x1 - 1, y1 - 1)):
            tested += 1
            gx, gy = env.pointer_roundtrip(x, y)
            if abs(gx - x) > POINTER_TOLERANCE_PX or abs(gy - y) > POINTER_TOLERANCE_PX:
                bad.append(f"screen {n} {label}: asked ({x},{y}) got ({gx},{gy})")
    if tested == 0:
        return FAIL, "no screens reported", {}
    return (FAIL, "the pointer does not land where the mapper says: " + "; ".join(bad[:4]), {"bad": bad}) if bad else \
        (PASS, f"pointer landed within {POINTER_TOLERANCE_PX}px at {tested} points on {tested // 3} screen(s)", {"points": tested})


def check_taskbar(env: GestureEnv) -> tuple[str, str]:
    reach = env.taskbar_reachable()
    if reach is None:
        return SKIP, "no taskbar information"
    return (PASS, "the cursor area includes the taskbar (whole monitor)") if reach else (WARN, "the cursor area is the work area: the taskbar cannot be reached")


def run(env: GestureEnv, *, hand_check: bool = True) -> Report:
    report = Report("gesture")
    first = timed("platform", lambda: check_platform(env))
    report.add(first)
    if first.status == PENDING:
        return report
    report.add(timed("mediapipe on this Python", lambda: check_python_wheel(env)))
    model = report.add(timed("hand model file", lambda: check_model(env)))
    report.add(timed("DPI awareness", lambda: check_dpi(env)))
    report.add(timed("pointer lands where the mapper says (moves the mouse, no click)", lambda: check_pointer(env)))
    report.add(timed("cursor area includes the taskbar", lambda: check_taskbar(env)))
    cam = report.add(timed("webcam delivers frames", lambda: check_camera(env)))
    if cam.status == FAIL or model.status == FAIL or report.results[1].status == FAIL:
        report.add(Result("real hand detection", SKIP, "needs mediapipe, the model and a working webcam (see above)"))
    elif not hand_check:
        report.add(Result("real hand detection", PENDING, "run without --no-hand to hold a hand up and measure detection"))
    else:
        run_ = env.detect(6.0)
        report.add(timed("real hand detection", lambda: check_detection(run_)))
    report.add(Result("real click lands on the right control", PENDING,
                      "by design not automated: start practice mode, then real control, and confirm clicks land where you point"))
    return report


class RealGestureEnv(GestureEnv):
    """Real hardware. NOT run by the automated tests: this is exactly what ``--validate gesture`` exercises on your PC."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def is_windows(self) -> bool:
        import sys

        return sys.platform == "win32"

    def python_version(self) -> tuple[int, int]:
        import sys

        return sys.version_info[0], sys.version_info[1]

    def mediapipe(self) -> tuple[bool, str]:
        try:
            import mediapipe as mp

            return True, getattr(mp, "__version__", "?")
        except Exception as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    def model_ready(self) -> tuple[bool, str]:
        from app.gesture.detector import availability, model_path

        ok, why = availability(self.ctx.paths.models_dir)
        return (True, str(model_path(self.ctx.paths.models_dir))) if ok else (False, why or "not ready")

    def camera_frames(self, seconds: float) -> tuple[int, str]:
        import time

        from app.gesture.camera import CameraBroker

        def opener():
            from app.vision.presence import Camera

            return Camera()

        broker = CameraBroker(opener, fps=30)
        try:
            lease = broker.lease()
        except Exception as exc:
            return 0, str(exc)
        count, end = 0, time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                if lease.frame() is not None:
                    count += 1
                time.sleep(0.005)
        finally:
            lease.close()
        return count, broker.error

    def detect(self, seconds: float) -> DetectRun:
        import time

        from app.gesture.camera import CameraBroker
        from app.gesture.detector import MediaPipeDetector

        print("Hold one hand up to the camera, fingers visible, for the next few seconds...", flush=True)
        detector = MediaPipeDetector(self.ctx.paths.models_dir)
        from app.vision.presence import Camera

        broker = CameraBroker(Camera, fps=30)
        lease = broker.lease()
        frames = with_hand = 0
        lat: list[float] = []
        start = time.monotonic()
        try:
            while time.monotonic() - start < seconds:
                frame = lease.frame()
                if frame is None:
                    time.sleep(0.003)
                    continue
                t0 = time.monotonic()
                hand = detector.detect(frame, int(t0 * 1000))
                lat.append((time.monotonic() - t0) * 1000)
                frames += 1
                with_hand += hand is not None
        finally:
            lease.close()
            detector.close()
        return DetectRun(frames, with_hand, time.monotonic() - start, lat)

    def dpi_awareness(self) -> str:
        from app.tools import winapi

        return winapi.ensure_dpi_aware()    # what gesture control itself does before it moves the mouse

    def screens(self) -> list[tuple[int, int, int, int]]:
        from app.tools import winapi

        return [((m.full or m.area).left, (m.full or m.area).top, (m.full or m.area).right, (m.full or m.area).bottom) for m in winapi.monitors()]

    def pointer_roundtrip(self, x: int, y: int) -> tuple[int, int]:
        import ctypes
        from ctypes import wintypes

        from app.tools import winapi

        winapi.mouse_move(x, y)
        point = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def taskbar_reachable(self) -> bool | None:
        from app.tools import winapi

        mons = winapi.monitors()
        return all(m.full is not None for m in mons) if mons else None
