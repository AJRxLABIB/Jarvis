"""Gesture engine: landmarks in, gestures and edge-triggered events out. Never touches the OS or the LLM.

Geometry + temporal filtering (a new pose must hold for STABLE_FRAMES) + hysteresis (pinch starts below PINCH_ON, ends above PINCH_OFF).
Pinch is an explicit state machine: NOT_PINCHED -> PINCH_STARTED -> PINCH_HELD -> PINCH_RELEASED -> NOT_PINCHED, so a held pinch clicks once."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from app.gesture.landmarks import (INDEX_TIP, MIDDLE_TIP, THUMB_TIP, WRIST, Hand, dist, extended_fingers, hand_size, middle_pinch_ratio, palm_centre,
                                   pinch_ratio)

NONE, INDEX_POINT, PINCH, OPEN_PALM, TWO_FINGER, FIST, UNKNOWN = "NONE", "INDEX_POINT", "PINCH", "OPEN_PALM", "TWO_FINGER_SCROLL", "FIST", "UNKNOWN"
NOT_PINCHED, PINCH_STARTED, PINCH_HELD, PINCH_RELEASED = "NOT_PINCHED", "PINCH_STARTED", "PINCH_HELD", "PINCH_RELEASED"
BASIC = (INDEX_POINT, PINCH, OPEN_PALM, TWO_FINGER)


@dataclass
class GestureConfig:
    pinch_on: float = 0.28
    pinch_off: float = 0.42
    stable_frames: int = 2
    pinch_confirm: int = 2          # consecutive frames under the pinch threshold before a pinch starts (one noisy frame is not a click)
    min_confidence: float = 0.6
    palm_hold_s: float = 0.9
    click_max_s: float = 0.8
    drag_hold_s: float = 0.45
    drag_enabled: bool = False
    fist_drag: bool = False
    double_click: bool = False
    right_click: bool = False       # thumb + middle finger pinch (index NOT pinched), edge-triggered like the left click
    double_click_s: float = 0.4
    scroll_gain: float = 14.0       # notches per hand-height of movement
    scroll_deadzone: float = 0.012
    lost_timeout_s: float = 0.35
    mirror: bool = True             # the user's right is the camera image's left
    dwell_click: bool = False       # hold the fingertip still for dwell_s to click (accessibility; off by default)
    dwell_s: float = 1.2
    dwell_radius: float = 0.35      # in hand sizes
    swipe_travel: float = 1.6       # an open-palm swipe must travel this many hand sizes ...
    swipe_window_s: float = 0.6     # ... within this time ...
    swipe_dominance: float = 0.7    # ... mostly sideways (diagonals are ignored) ...
    swipe_cooldown_s: float = 1.0   # ... and cannot repeat sooner than this
    zoom_step: float = 0.12         # change in the distance between two pinching hands (log ratio) per zoom notch
    zoom: bool = True
    enabled_gestures: frozenset = frozenset(BASIC)


@dataclass
class GestureFrame:
    gesture: str = NONE
    pinch: str = NOT_PINCHED
    point: tuple[float, float] | None = None  # normalised image position that drives the cursor
    events: list = field(default_factory=list)  # ("click",) ("double_click",) ("drag_start",) ("drag_end",) ("scroll", n) ("palm_hold",) ("lost",)
    confidence: float = 0.0
    hands: list = field(default_factory=list)   # every hand seen this frame (primary first)
    dwell: float = 0.0                           # 0..1 progress of a dwell click, for the preview


def raw_gesture(h: Hand, cfg: GestureConfig, pinching: bool) -> str:
    if pinching:
        return PINCH
    ext = extended_fingers(h)
    if ext == {"index"}:
        return INDEX_POINT
    if ext == {"index", "middle"}:
        return TWO_FINGER
    if len(ext) == 4:
        return OPEN_PALM
    if not ext:
        return FIST
    return UNKNOWN


class GestureEngine:
    def __init__(self, cfg: GestureConfig | None = None) -> None:
        self.cfg = cfg or GestureConfig()
        self.reset()

    def reset(self) -> None:
        self.pinch_state = NOT_PINCHED
        self._pinching = False
        self._candidate, self._count, self.gesture = NONE, 0, NONE
        self._pinch_at = 0.0
        self._dragging = False
        self._last_click = -9.0
        self._palm_since: float | None = None
        self._palm_fired = False
        self._scroll_y: float | None = None
        self._scroll_acc = 0.0
        self._last_seen = None
        self._lost_fired = True
        self._rpinch = False
        self._rpinch_at = 0.0
        self._armed = False                          # Midas-touch guard: a hand that appears already pinched must open first
        self._palm_hist: deque = deque(maxlen=24)
        self._palm_anchor: tuple[float, float] | None = None
        self._last_swipe = -9.0
        self._dwell_anchor: tuple[float, float] | None = None
        self._dwell_since = 0.0
        self._dwell_fired = False
        self._pinch_frames = 0

    @property
    def dragging(self) -> bool:
        return self._dragging

    def update(self, hand: Hand | None, now: float) -> GestureFrame:
        cfg, out = self.cfg, GestureFrame()
        if hand is None or hand.confidence < cfg.min_confidence:
            return self._lost(now, out)
        self._last_seen, self._lost_fired = now, False
        out.confidence = hand.confidence
        ratio = pinch_ratio(hand)
        if not self._armed:                             # entered the view (or resumed) already pinching: ignore until the hand opens once
            if ratio < cfg.pinch_on:
                self._pinching, self._pinch_frames = False, 0
            else:
                self._armed = True
        if self._armed:
            if self._pinching:
                self._pinching = ratio < cfg.pinch_off      # hysteresis: harder to let go than to start
            else:
                self._pinch_frames = self._pinch_frames + 1 if ratio < cfg.pinch_on else 0
                self._pinching = self._pinch_frames >= cfg.pinch_confirm
        raw = raw_gesture(hand, cfg, self._pinching)
        if raw == self._candidate:
            self._count += 1
        else:
            self._candidate, self._count = raw, 1
        if self._count >= cfg.stable_frames or raw == PINCH or self.gesture == PINCH:
            if raw != self.gesture and (self._count >= cfg.stable_frames or raw == PINCH):
                self.gesture = raw
        out.gesture = self.gesture if self.gesture in cfg.enabled_gestures or self.gesture in (FIST, UNKNOWN) else UNKNOWN
        out.point = hand.xy(INDEX_TIP) if out.gesture != PINCH else ((hand.xy(INDEX_TIP)[0] + hand.xy(THUMB_TIP)[0]) / 2,
                                                                      (hand.xy(INDEX_TIP)[1] + hand.xy(THUMB_TIP)[1]) / 2)
        self._pinch(out, now, enabled=PINCH in cfg.enabled_gestures)
        self._right(hand, out, now)
        self._fist(out, now)
        self._palm(hand, out, now)
        self._swipe(hand, out, now)
        self._dwell(hand, out, now)
        self._scroll(hand, out)
        return out

    # ---- pieces ----------------------------------------------------------------------------------------------------------------------------
    def _lost(self, now: float, out: GestureFrame) -> GestureFrame:
        if self._last_seen is not None and not self._lost_fired and now - self._last_seen >= self.cfg.lost_timeout_s:
            self._lost_fired = True
            out.events.append(("lost",))
            self._reset_after_loss(out)
        return out

    def _reset_after_loss(self, out: GestureFrame) -> None:
        if self._dragging:
            self._dragging = False
            out.events.append(("drag_end",))
        self.pinch_state, self._pinching, self.gesture, self._candidate, self._count = NOT_PINCHED, False, NONE, NONE, 0
        self._pinch_frames = 0
        self._palm_since, self._scroll_y, self._armed = None, None, False
        self._palm_hist.clear()
        self._dwell_anchor = None

    def _pinch(self, out: GestureFrame, now: float, enabled: bool) -> None:
        pinching = self._pinching and enabled
        prev = self.pinch_state
        if pinching and prev in (NOT_PINCHED, PINCH_RELEASED):
            self.pinch_state, self._pinch_at = PINCH_STARTED, now
        elif pinching:
            self.pinch_state = PINCH_HELD
            if self.cfg.drag_enabled and not self._dragging and now - self._pinch_at >= self.cfg.drag_hold_s:
                self._dragging = True
                out.events.append(("drag_start",))
        elif prev in (PINCH_STARTED, PINCH_HELD):
            self.pinch_state = PINCH_RELEASED
            if self._dragging:
                self._dragging = False
                out.events.append(("drag_end",))
            elif now - self._pinch_at <= self.cfg.click_max_s:
                if self.cfg.double_click and now - self._last_click <= self.cfg.double_click_s:
                    out.events.append(("double_click",))
                    self._last_click = -9.0
                else:
                    out.events.append(("click",))
                    self._last_click = now
        else:
            self.pinch_state = NOT_PINCHED
        out.pinch = self.pinch_state

    def _right(self, hand: Hand, out: GestureFrame, now: float) -> None:
        if not self.cfg.right_click or self._pinching or self.pinch_state in (PINCH_STARTED, PINCH_HELD):
            self._rpinch = False
            return
        ratio = middle_pinch_ratio(hand)
        if not self._rpinch and ratio < self.cfg.pinch_on:
            self._rpinch, self._rpinch_at = True, now
        elif self._rpinch and ratio > self.cfg.pinch_off:
            self._rpinch = False
            if now - self._rpinch_at <= self.cfg.click_max_s:
                out.events.append(("right_click",))

    def _fist(self, out: GestureFrame, now: float) -> None:
        if not self.cfg.fist_drag:
            return
        if out.gesture == FIST and not self._dragging and self.pinch_state == NOT_PINCHED:
            self._dragging = True
            out.events.append(("drag_start",))
        elif out.gesture != FIST and self._dragging and self.pinch_state == NOT_PINCHED:
            self._dragging = False
            out.events.append(("drag_end",))

    def _palm(self, hand: Hand, out: GestureFrame, now: float) -> None:
        if out.gesture == OPEN_PALM and OPEN_PALM in self.cfg.enabled_gestures:
            centre = palm_centre(hand)
            if self._palm_anchor is None or dist(centre, self._palm_anchor) > hand_size(hand) * 0.6:
                self._palm_anchor, self._palm_since = centre, None         # moving (a swipe), not a held palm: restart the hold
            if self._palm_since is None:
                self._palm_since, self._palm_fired = now, False
            elif not self._palm_fired and now - self._palm_since >= self.cfg.palm_hold_s:
                self._palm_fired = True
                out.events.append(("palm_hold",))
        else:
            self._palm_since, self._palm_anchor = None, None

    def _swipe(self, hand: Hand, out: GestureFrame, now: float) -> None:
        """A quick sideways sweep of an open palm: ("swipe", "left"|"right") as the USER sees it. Needs travel, a dominant direction and a cooldown."""
        cfg = self.cfg
        if out.gesture != OPEN_PALM:
            self._palm_hist.clear()
            return
        cx, cy = palm_centre(hand)
        self._palm_hist.append((now, cx, cy, hand_size(hand)))
        while self._palm_hist and now - self._palm_hist[0][0] > cfg.swipe_window_s:
            self._palm_hist.popleft()
        if len(self._palm_hist) < 3 or now - self._last_swipe < cfg.swipe_cooldown_s:
            return
        t0, x0, y0, size = self._palm_hist[0]
        dx, dy = cx - x0, cy - y0
        total = abs(dx) + abs(dy)
        if total > 0 and abs(dx) >= cfg.swipe_travel * size and abs(dx) / total >= cfg.swipe_dominance:
            right = dx < 0 if cfg.mirror else dx > 0
            out.events.append(("swipe", "right" if right else "left"))
            self._last_swipe = now
            self._palm_hist.clear()
            self._palm_since = None            # a swipe is not a "hold palm to pause"

    def _dwell(self, hand: Hand, out: GestureFrame, now: float) -> None:
        cfg = self.cfg
        if not cfg.dwell_click or out.gesture != INDEX_POINT:
            self._dwell_anchor = None
            return
        tip, size = hand.xy(INDEX_TIP), hand_size(hand)
        if self._dwell_anchor is None or dist(tip, self._dwell_anchor) > cfg.dwell_radius * size:
            self._dwell_anchor, self._dwell_since, self._dwell_fired = tip, now, False
            return
        out.dwell = min(1.0, (now - self._dwell_since) / cfg.dwell_s)
        if not self._dwell_fired and now - self._dwell_since >= cfg.dwell_s:
            self._dwell_fired = True            # one click per stay; moving away re-arms it
            out.events.append(("click",))

    def _scroll(self, hand: Hand, out: GestureFrame) -> None:
        if out.gesture != TWO_FINGER:
            self._scroll_y, self._scroll_acc = None, 0.0
            return
        y = (hand.xy(INDEX_TIP)[1] + hand.xy(MIDDLE_TIP)[1]) / 2
        if self._scroll_y is not None:
            delta = (self._scroll_y - y) / max(1e-6, hand_size(hand) * 2.5)  # fingers up = scroll up
            if abs(delta) >= self.cfg.scroll_deadzone:
                self._scroll_acc += delta * self.cfg.scroll_gain
            whole = int(self._scroll_acc)
            if whole:
                self._scroll_acc -= whole
                out.events.append(("scroll", whole))
        self._scroll_y = y
        out.point = None  # the cursor does not move while scrolling


class MultiHandEngine:
    """Up to two hands: one hand drives the pointer (the primary, kept by continuity so a second hand entering the view cannot steal it), and two
    pinching hands moving apart / together make a zoom (events ("zoom", +n) in, ("zoom", -n) out). While zooming, clicks and drags are suppressed."""

    def __init__(self, cfg: GestureConfig | None = None) -> None:
        self.cfg = cfg or GestureConfig()
        self.primary = GestureEngine(self.cfg)
        self.reset()

    def reset(self) -> None:
        self.primary.reset()
        self._wrist: tuple[float, float] | None = None
        self._zoom_ref: float | None = None
        self._zoom_frames = 0
        self._zoom_until = -9.0
        self.zooming = False

    @property
    def dragging(self) -> bool:
        return self.primary.dragging

    def _pick(self, hands: list[Hand]) -> tuple[Hand | None, Hand | None]:
        hands = [h for h in hands if h.confidence >= self.cfg.min_confidence]
        if not hands:
            return None, None
        if len(hands) == 1:
            return hands[0], None
        if self._wrist is not None:
            first = min(hands, key=lambda h: dist(h.xy(WRIST), self._wrist))
        else:
            first = max(hands, key=lambda h: (extended_fingers(h) == {"index"}, h.confidence))
        other = next(h for h in hands if h is not first)
        return first, other

    @staticmethod
    def _pinch_point(h: Hand) -> tuple[float, float]:
        a, b = h.xy(INDEX_TIP), h.xy(THUMB_TIP)
        return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2

    def update(self, hands: list[Hand] | Hand | None, now: float) -> GestureFrame:
        if hands is None:
            hands = []
        elif isinstance(hands, Hand):
            hands = [hands]
        cfg = self.cfg
        first, other = self._pick(list(hands))
        out = self.primary.update(first, now)
        out.hands = [h for h in (first, other) if h is not None]
        if first is not None:
            self._wrist = first.xy(WRIST)
        else:
            self._wrist = None
        events = []
        if cfg.zoom and first is not None and other is not None:
            limit = cfg.pinch_off if self.zooming else cfg.pinch_on
            both = pinch_ratio(first) < limit and pinch_ratio(other) < limit
            if both:
                d = dist(self._pinch_point(first), self._pinch_point(other))
                self._zoom_frames += 1
                if not self.zooming and self._zoom_frames >= 2:
                    self.zooming, self._zoom_ref = True, max(d, 1e-3)
                elif self.zooming:
                    step = math.log(max(d, 1e-3) / self._zoom_ref)
                    notches = int(step / cfg.zoom_step)
                    if notches:
                        notches = max(-3, min(3, notches))
                        events.append(("zoom", notches))
                        self._zoom_ref = max(d, 1e-3)
            else:
                self._zoom_frames, self.zooming = 0, False
        else:
            self._zoom_frames, self.zooming = 0, False
        if self.zooming or self._zoom_frames:
            self._zoom_until = now + 0.5
        if self.zooming or now < self._zoom_until:
            # two hands pinching is a zoom, not a click: drop what the pointer hand would do, and do not move the pointer
            out.events = [e for e in out.events if e[0] not in ("click", "double_click", "right_click", "drag_start", "palm_hold", "scroll")]
            out.point = None
        out.events.extend(events)
        return out
