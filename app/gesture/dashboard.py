"""Gesture Control dashboard model, calibration wizard and tutorial: pure logic (no Qt) so it is unit-tested; the Qt page only draws it.

Only measured values are shown (frames, fps, detection milliseconds); anything not measured says so."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.gesture.cursor import Calibration
from app.gesture.engine import GestureFrame
from app.gesture.landmarks import INDEX_TIP, Hand, dist

# MediaPipe hand skeleton (pairs of landmark indices)
CONNECTIONS = ((0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15),
               (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17))


def skeletons(hands: list, width: int, height: int, *, mirror: bool = True) -> list[tuple[list, list]]:
    """One (points, bones) pair per hand for the preview (up to two hands)."""
    return [skeleton(h, width, height, mirror=mirror) for h in hands[:2]]


def skeleton(hand: Hand | None, width: int, height: int, *, mirror: bool = True) -> tuple[list[tuple[int, int]], list[tuple[tuple[int, int], tuple[int, int]]]]:
    """Landmark pixels and the bone segments for a preview of size width x height (mirrored like a mirror, matching the cursor)."""
    if hand is None:
        return [], []
    pts = [((1.0 - p[0]) if mirror else p[0], p[1]) for p in hand.points]
    px = [(int(max(0.0, min(1.0, x)) * (width - 1)), int(max(0.0, min(1.0, y)) * (height - 1))) for x, y in pts]
    return px, [(px[a], px[b]) for a, b in CONNECTIONS]


def panels(status: dict, *, ready: tuple[bool, str], allowed: bool, settings: dict) -> dict[str, list[str]]:
    """The six dashboard panels as plain lines."""
    stats = status.get("stats", {})

    def val(key: str, unit: str = "") -> str:
        v = stats.get(key)
        return "not measured yet" if v is None else f"{v}{unit}"

    return {
        "Camera": [f"State: {status.get('camera', 'STOPPED')}", "Local only; frames are never saved",
                   "CAMERA ACTIVE indicator: " + ("on" if status.get("running") else "off")],
        "Hand tracking": [f"Detector: {'ready' if ready[0] else 'NOT ready - ' + ready[1]}", f"Frames processed: {stats.get('frames_processed', 0)}",
                          f"Frames with a hand: {stats.get('frames_with_hand', 0)}", f"Last gesture: {stats.get('last_gesture', 'NONE')}"],
        "Virtual mouse": [f"Control: {status.get('state', 'DISABLED')} ({status.get('reason', '')})",
                          "Practice mode (nothing is clicked)" if status.get("practice") else "Real mouse control" if status.get("running") else "Not running",
                          f"A button is being held: {'yes' if status.get('holding_button') else 'no'}"],
        "Mappings": [f"{g}: runs {m['tool']} ({'asks first' if m['confirm'] else 'no question'}, {'on' if m['enabled'] else 'off'})"
                     for g, m in status.get("mappings", {}).items()] or ["No gesture runs a Jarvis action (default)."],
        "Performance": [f"Frame rate (measured): {val('fps_measured', ' fps')}", f"Detection time, median: {val('detect_ms_median', ' ms')}",
                        f"Detection time, 95th percentile: {val('detect_ms_p95', ' ms')}"],
        "Safety": ["Gesture control is " + ("allowed in Settings" if allowed else "OFF in Settings > Privacy (nothing can start)"),
                   "Emergency stop: the red Stop button, Ctrl+Shift+F12, or say 'emergency stop'", "Open palm held ~1 s pauses control",
                   f"Problem: {status['problem']}" if status.get("problem") else "No problem reported",
                   "Drag: " + ("on" if settings.get("drag") else "off") + "; right click: " + ("on" if settings.get("right_click") else "off")
                   + "; zoom: " + ("on" if settings.get("zoom", True) else "off") + "; swipe: " + str(settings.get("swipe", "off"))
                   + "; dwell click: " + ("on" if settings.get("dwell_click") else "off")],
    }


# ---- calibration wizard -----------------------------------------------------------------------------------------------------------------------
@dataclass
class CalibrationWizard:
    """Two steps: hold your fingertip where you want the TOP-LEFT of the screen, then the BOTTOM-RIGHT. Each is captured when the fingertip stays
    within `radius` for `hold_s`. Coordinates are raw camera-image coordinates, which is what the cursor expects."""
    hold_s: float = 1.0
    radius: float = 0.03
    step: int = 0                      # 0 top-left, 1 bottom-right, 2 done
    corners: list = field(default_factory=list)
    _anchor: tuple[float, float] | None = None
    _since: float = 0.0
    result: Calibration | None = None
    message: str = "Point at where the TOP-LEFT corner of your screen should be and hold still."

    PROMPTS = ("Point at where the TOP-LEFT corner of your screen should be and hold still.",
               "Now point at where the BOTTOM-RIGHT corner should be and hold still.")

    @property
    def done(self) -> bool:
        return self.step >= 2

    def progress(self, now: float) -> float:
        return 0.0 if self._anchor is None else min(1.0, (now - self._since) / self.hold_s)

    def feed(self, hand: Hand | None, now: float) -> None:
        if self.done:
            return
        if hand is None:
            self._anchor = None
            return
        point = hand.xy(INDEX_TIP)
        if self._anchor is None or dist(point, self._anchor) > self.radius:
            self._anchor, self._since = point, now
            return
        if now - self._since >= self.hold_s:
            self.corners.append(self._anchor)
            self._anchor = None
            if len(self.corners) == 1:
                self.step, self.message = 1, self.PROMPTS[1]
                return
            cal = Calibration.from_corners(self.corners[0], self.corners[1])
            if cal is None:
                self.corners, self.step = [], 0
                self.message = self.PROMPTS[0] + " (The last two points were too close together.)"
            else:
                self.step, self.result, self.message = 2, cal, "Calibration captured. Save it, or start over."

    def restart(self) -> None:
        self.step, self.corners, self._anchor, self.result, self.message = 0, [], None, None, self.PROMPTS[0]


# ---- tutorial ---------------------------------------------------------------------------------------------------------------------------------
STEPS = (
    ("point", "Point with your index finger and move your hand. Watch the skeleton follow.", "INDEX_POINT"),
    ("pinch", "Touch your thumb and index finger together once, then let go: that is a click.", "click"),
    ("scroll", "Raise index and middle fingers and move your hand up or down: that scrolls.", "scroll"),
    ("palm", "Show an open palm and hold it still about one second: that pauses control.", "palm_hold"),
    ("swipe", "Sweep an open palm quickly sideways: that is a swipe (it only does something if you chose a swipe action).", "swipe"),
    ("zoom", "Pinch with BOTH hands and move them apart or together: that zooms.", "zoom"),
)


@dataclass
class Tutorial:
    """Practice mode only (nothing is clicked): advances when the gesture the step asks for is really seen."""
    index: int = 0
    moved: int = 0
    _first: tuple[float, float] | None = None

    @property
    def done(self) -> bool:
        return self.index >= len(STEPS)

    @property
    def prompt(self) -> str:
        return "Done. You know every gesture. Turn real control on when you are ready." if self.done else f"Step {self.index + 1}/{len(STEPS)}: {STEPS[self.index][1]}"

    def feed(self, frame: GestureFrame) -> bool:
        """Returns True when this frame completed the current step."""
        if self.done:
            return False
        _, _, want = STEPS[self.index]
        ok = False
        if want == "INDEX_POINT":
            if frame.gesture == "INDEX_POINT" and frame.point:
                if self._first is None:
                    self._first = frame.point
                ok = dist(frame.point, self._first) > 0.08
        else:
            ok = any(e[0] == want for e in frame.events)
        if ok:
            self.index += 1
            self._first = None
        return ok
