"""Seeing and controlling the phone's screen from the PC (JARVIS Mobile "Share screen").

How it works - and who decides:
* The PHONE starts it: you press "Share screen with PC" in JARVIS Mobile and Android itself asks "Start recording
  or casting?" every time (MediaProjection). A notification stays up while sharing; Stop on the phone ends it.
* The phone sends small JPEG frames (signed, pinned TLS, max ~4 a second) to ``/api/mobile/frame``. Frames live only
  in memory here - never written to disk, never sent anywhere else - and are dropped when sharing stops.
* Control is a second, separate opt-in on the phone: the JARVIS accessibility service (Android Settings >
  Accessibility) plus "Allow the PC to tap" in the app. Only then does the phone carry out the taps / swipes /
  Back / Home / Recents you make in the PC's viewer; each frame upload collects the waiting input.
* The PC side can stop it too (the viewer's Stop): the next frame answer tells the phone to stop.

Everything is per paired, trusted phone; an untrusted or revoked phone's frames are refused by the signature check.
"""

from __future__ import annotations

import base64
import threading
import time
from collections import deque
from dataclasses import dataclass, field

MAX_FRAME_BYTES = 450_000          # one JPEG frame (the phone sends ~540 px wide, quality 60: usually 30-90 KB)
STALE_SECONDS = 6.0                # no frame for this long: the phone stopped sharing (or lost the network)
MAX_INPUT = 32
INPUT_KINDS = ("tap", "swipe", "back", "home", "recents", "text")


class ScreenError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Share:
    device_id: str
    name: str
    started: float
    frame: bytes = b""
    width: int = 0
    height: int = 0
    at: float = 0.0
    frames: int = 0
    control: bool = False                          # the phone says its accessibility service is on and allowed
    stop_requested: bool = False
    pending: deque = field(default_factory=lambda: deque(maxlen=MAX_INPUT))


def _clamp01(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ScreenError("malformed", "coordinates must be numbers") from None
    if v != v:  # NaN
        raise ScreenError("malformed", "coordinates must be numbers")
    return max(0.0, min(1.0, v))


def make_input(kind: str, **values) -> dict:
    """A validated input event (coordinates are 0..1 of the phone's screen, so any size works)."""
    if kind not in INPUT_KINDS:
        raise ScreenError("malformed", f"unknown input {kind!r}")
    event: dict = {"kind": kind}
    if kind == "tap":
        event.update(x=_clamp01(values.get("x")), y=_clamp01(values.get("y")))
        event["long"] = bool(values.get("long", False))
    elif kind == "swipe":
        event.update(x=_clamp01(values.get("x")), y=_clamp01(values.get("y")),
                     x2=_clamp01(values.get("x2")), y2=_clamp01(values.get("y2")))
        ms = values.get("ms", 300)
        event["ms"] = int(max(50, min(2000, ms if isinstance(ms, (int, float)) else 300)))
    elif kind == "text":
        text = str(values.get("text", ""))[:500]
        if not text:
            raise ScreenError("malformed", "empty text")
        event["text"] = text
    return event


class PhoneScreenHub:
    """Thread-safe: frames arrive on the server's thread, the viewer reads them on the window's thread."""

    def __init__(self, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._shares: dict[str, Share] = {}
        self._clock = clock
        self.on_change = None          # callable(device_id) -> None: the viewer repaints (called on any thread)

    # ---- from the phone (signed request, already verified)
    def frame(self, device, arguments: dict) -> dict:
        data = arguments.get("jpeg", "")
        if not isinstance(data, str) or not data:
            raise ScreenError("malformed", "no frame")
        try:
            jpeg = base64.b64decode(data, validate=True)
        except ValueError:
            raise ScreenError("malformed", "the frame isn't base64") from None
        if len(jpeg) > MAX_FRAME_BYTES:
            raise ScreenError("too_big", "frame too big")
        if not jpeg.startswith(b"\xff\xd8"):
            raise ScreenError("malformed", "the frame isn't a JPEG")
        width, height = arguments.get("width", 0), arguments.get("height", 0)
        now = self._clock()
        with self._lock:
            share = self._shares.get(device.id)
            if share is None or now - share.at > STALE_SECONDS and share.frames:
                share = self._shares[device.id] = Share(device.id, getattr(device, "name", "") or "phone", now)
            share.frame, share.at, share.frames = jpeg, now, share.frames + 1
            share.width = int(width) if isinstance(width, int) and 0 < width < 10000 else 0
            share.height = int(height) if isinstance(height, int) and 0 < height < 10000 else 0
            share.control = bool(arguments.get("control", False))
            stop = share.stop_requested
            events = list(share.pending) if share.control and not stop else []
            share.pending.clear()
            if stop:
                self._shares.pop(device.id, None)
        if self.on_change is not None:
            self.on_change(device.id)
        return {"ok": True, "stop": stop, "input": events}

    def stopped(self, device) -> dict:
        with self._lock:
            self._shares.pop(device.id, None)
        if self.on_change is not None:
            self.on_change(device.id)
        return {"ok": True}

    # ---- from the PC's viewer
    def active(self) -> list[Share]:
        now = self._clock()
        with self._lock:
            return [s for s in self._shares.values() if now - s.at <= STALE_SECONDS and not s.stop_requested]

    def latest(self, device_id: str) -> Share | None:
        with self._lock:
            return self._shares.get(device_id)

    def send_input(self, device_id: str, kind: str, **values) -> bool:
        event = make_input(kind, **values)
        with self._lock:
            share = self._shares.get(device_id)
            if share is None or not share.control:
                return False
            share.pending.append(event)
            return True

    def stop(self, device_id: str) -> None:
        with self._lock:
            share = self._shares.get(device_id)
            if share is not None:
                share.stop_requested = True
                share.frame = b""
                share.pending.clear()

    def stop_all(self) -> None:
        with self._lock:
            for share in self._shares.values():
                share.stop_requested = True
                share.frame = b""
                share.pending.clear()
