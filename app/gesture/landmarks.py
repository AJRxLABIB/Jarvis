"""Hand landmarks (MediaPipe 21-point layout) and the geometry used to recognise gestures. Pure functions, no hardware."""

from __future__ import annotations

import math
from dataclasses import dataclass

WRIST, THUMB_CMC, THUMB_MCP, THUMB_IP, THUMB_TIP = 0, 1, 2, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP = 5, 6, 7, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP = 9, 10, 11, 12
RING_MCP, RING_PIP, RING_DIP, RING_TIP = 13, 14, 15, 16
PINKY_MCP, PINKY_PIP, PINKY_DIP, PINKY_TIP = 17, 18, 19, 20
FINGERS = {"index": (INDEX_MCP, INDEX_PIP, INDEX_TIP), "middle": (MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP),
           "ring": (RING_MCP, RING_PIP, RING_TIP), "pinky": (PINKY_MCP, PINKY_PIP, PINKY_TIP)}


@dataclass(frozen=True)
class Hand:
    points: tuple[tuple[float, float, float], ...]  # 21 x (x, y, z), x/y normalised 0..1 in the camera image
    confidence: float = 1.0
    handedness: str = "unknown"  # "left" / "right" / "unknown"

    def __post_init__(self) -> None:
        if len(self.points) != 21:
            raise ValueError("a hand needs exactly 21 landmarks")
        if not all(math.isfinite(v) for p in self.points for v in p):
            raise ValueError("landmarks must be finite numbers")

    def xy(self, i: int) -> tuple[float, float]:
        return self.points[i][0], self.points[i][1]


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def hand_size(h: Hand) -> float:
    """Scale reference (wrist to middle-finger knuckle, plus palm width), so thresholds hold at any distance from the camera."""
    return max(1e-6, (dist(h.xy(WRIST), h.xy(MIDDLE_MCP)) + dist(h.xy(INDEX_MCP), h.xy(PINKY_MCP))) / 2)


def pinch_ratio(h: Hand) -> float:
    """Thumb-tip to index-tip distance in hand sizes: small = pinching."""
    return dist(h.xy(THUMB_TIP), h.xy(INDEX_TIP)) / hand_size(h)


def middle_pinch_ratio(h: Hand) -> float:
    """Thumb-tip to middle-tip distance in hand sizes (the right-click pinch)."""
    return dist(h.xy(THUMB_TIP), h.xy(MIDDLE_TIP)) / hand_size(h)


def finger_extended(h: Hand, name: str) -> bool:
    mcp, pip, tip = FINGERS[name]
    wrist = h.xy(WRIST)
    # extended: the tip is clearly farther from the wrist than the middle joint (works for a rotated hand)
    return dist(wrist, h.xy(tip)) > dist(wrist, h.xy(pip)) * 1.12 and dist(h.xy(tip), h.xy(mcp)) > dist(h.xy(pip), h.xy(mcp)) * 1.2


def finger_curled(h: Hand, name: str) -> bool:
    mcp, pip, tip = FINGERS[name]
    wrist = h.xy(WRIST)
    return dist(wrist, h.xy(tip)) < dist(wrist, h.xy(pip)) * 1.02


def extended_fingers(h: Hand) -> set[str]:
    return {n for n in FINGERS if finger_extended(h, n)}


def palm_centre(h: Hand) -> tuple[float, float]:
    pts = [h.xy(i) for i in (WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)]
    return sum(p[0] for p in pts) / 5, sum(p[1] for p in pts) / 5


def synthetic_hand(*, extended=("index",), pinch: bool = False, middle_pinch: bool = False, at=(0.5, 0.5), size: float = 0.2, confidence: float = 1.0,
                   handedness: str = "right") -> Hand:
    """A geometrically plausible hand for tests / the practice mode (fingers pointing up in the image)."""
    ox, oy = at
    pts = [(ox, oy + size, 0.0)] * 21  # wrist at the bottom
    pts = list(pts)
    pts[WRIST] = (ox, oy + size * 0.9, 0.0)
    xs = {"index": -0.6, "middle": -0.2, "ring": 0.2, "pinky": 0.6}
    for name, (mcp, pip, tip) in FINGERS.items():
        x = ox + xs[name] * size * 0.5
        pts[mcp] = (x, oy + size * 0.3, 0.0)
        up = name in extended
        pts[pip] = (x, oy + (0.05 if up else 0.15) * size, 0.0)
        pts[mcp + 2] = (x, oy - (0.15 if up else -0.25) * size, 0.0)  # dip
        pts[tip] = (x, oy - (0.45 if up else -0.3) * size, 0.0) if up else (x, oy + size * 0.35, 0.0)
    pts[THUMB_CMC] = (ox - size * 0.3, oy + size * 0.6, 0.0)
    pts[THUMB_MCP] = (ox - size * 0.4, oy + size * 0.45, 0.0)
    pts[THUMB_IP] = (ox - size * 0.5, oy + size * 0.3, 0.0)
    pts[THUMB_TIP] = (ox - size * 0.55, oy + size * 0.1, 0.0)
    if pinch:
        pts[THUMB_TIP] = (pts[INDEX_TIP][0] - size * 0.02, pts[INDEX_TIP][1] + size * 0.02, 0.0)
    if middle_pinch:
        pts[THUMB_TIP] = (pts[MIDDLE_TIP][0] - size * 0.02, pts[MIDDLE_TIP][1] + size * 0.02, 0.0)
    return Hand(tuple(pts), confidence, handedness)
