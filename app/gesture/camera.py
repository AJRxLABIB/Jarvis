"""Shared webcam broker: one device, many consumers (presence detection, gesture control), explicit lifecycle.

State machine: STOPPED -> STARTING -> READY -> (TRACKING <-> PAUSED) ; CAMERA_UNAVAILABLE / ERROR ; STOPPING -> STOPPED.
The camera is opened only when something asks for a lease, and closed when the last lease is released. Only the NEWEST frame is kept
(a bounded slot of one), frames are never written anywhere, and a slow consumer drops frames instead of building a backlog."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

STOPPED, STARTING, READY, TRACKING, PAUSED, UNAVAILABLE, ERROR, STOPPING = (
    "STOPPED", "STARTING", "READY", "TRACKING", "PAUSED", "CAMERA_UNAVAILABLE", "ERROR", "STOPPING")
MAX_FAILED_READS = 30


class CameraBroker:
    def __init__(self, opener: Callable[[], object], *, fps: float = 30.0, on_active: Callable[[bool], None] | None = None) -> None:
        self._opener, self.fps, self.on_active = opener, fps, on_active
        self._lock = threading.Lock()
        self._leases = 0
        self._device = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frame = None
        self._seq = 0
        self._taken = 0
        self.state = STOPPED
        self.error = ""
        self.captured = 0
        self.dropped = 0
        self.started_at = 0.0

    # ---- leases ----------------------------------------------------------------------------------------------------------------------------
    def lease(self) -> Lease:
        with self._lock:
            if self._leases == 0:
                self._open()
            self._leases += 1
        return Lease(self)

    def _release(self) -> None:
        with self._lock:
            self._leases = max(0, self._leases - 1)
            if self._leases == 0:
                self._close()

    @property
    def leases(self) -> int:
        return self._leases

    def _open(self) -> None:
        self.state, self.error = STARTING, ""
        try:
            self._device = self._opener()
        except Exception as exc:
            self.state, self.error = UNAVAILABLE, f"No webcam could be opened ({exc})."
            self._device = None
            raise RuntimeError(self.error) from exc
        self._stop.clear()
        self._frame, self._seq, self._taken, self.captured, self.dropped = None, 0, 0, 0, 0
        self.started_at = time.monotonic()
        self.state = READY
        if self.on_active:
            self.on_active(True)
        self._thread = threading.Thread(target=self._run, name="camera-broker", daemon=True)
        self._thread.start()

    def _close(self) -> None:
        self.state = STOPPING
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        device, self._device, self._thread, self._frame = self._device, None, None, None
        try:
            if device is not None:
                device.close()
        finally:
            self.state = STOPPED if not self.error else self.state
            if self.on_active:
                self.on_active(False)

    def _run(self) -> None:
        failures = 0
        period = 1.0 / max(1.0, self.fps)
        while not self._stop.is_set():
            try:
                frame = self._device.frame()
            except Exception as exc:
                self.state, self.error = ERROR, f"The camera failed ({exc})."
                return
            if frame is None:
                failures += 1
                if failures >= MAX_FAILED_READS:
                    self.state, self.error = UNAVAILABLE, "The camera stopped delivering frames (unplugged or used by another app)."
                    return
                self._stop.wait(period)
                continue
            failures = 0
            if self._frame is not None and self._seq > self._taken:
                self.dropped += 1  # the previous frame was never consumed by anyone
            self._frame, self._seq, self.captured = frame, self._seq + 1, self.captured + 1
            self._stop.wait(period)

    def newest(self, after: int) -> tuple[object | None, int]:
        if self._seq > after:
            self._taken = self._seq
            return self._frame, self._seq
        return None, after

    def mark(self, state: str) -> None:
        if self.state in (READY, TRACKING, PAUSED):
            self.state = state

    @property
    def healthy(self) -> bool:
        return self.state in (READY, TRACKING, PAUSED)


class Lease:
    """What a consumer holds. Same shape as the old presence Camera: frame() and close()."""

    def __init__(self, broker: CameraBroker) -> None:
        self._broker, self._seen, self._open = broker, 0, True

    def frame(self):
        if not self._open:
            return None
        frame, self._seen = self._broker.newest(self._seen)
        return frame

    @property
    def healthy(self) -> bool:
        return self._open and self._broker.healthy

    def close(self) -> None:
        if self._open:
            self._open = False
            self._broker._release()


_BROKER: CameraBroker | None = None


def shared_broker(on_active: Callable[[bool], None] | None = None) -> CameraBroker:
    """The one broker for the real webcam (presence detection and gesture control both lease from it)."""
    global _BROKER
    if _BROKER is None:
        def opener():
            from app.vision.presence import Camera

            return Camera()

        _BROKER = CameraBroker(opener)
    if on_active is not None:
        _BROKER.on_active = on_active
    return _BROKER
