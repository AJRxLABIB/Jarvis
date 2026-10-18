"""Window / tab overview ("show me my screen", "show my tabs"): the pure parts - what goes in it, where each card
goes, and how cards move. Mission-Control style: every open window flies from where it really is into a tidy grid;
picking one zooms it back to its place and brings it to the front.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

Rect = tuple[float, float, float, float]          # x, y, width, height


@dataclass
class OverviewItem:
    key: str
    title: str
    subtitle: str = ""             # the app name ("Google Chrome") or, for a tab, the browser
    kind: str = "window"           # window | tab
    hwnd: int = 0                  # the window to show live (DWM) and to bring to the front
    source: Rect | None = None     # where it is on screen now (screen coordinates); None = minimised / unknown
    aspect: float = 16 / 10        # width / height of the card's picture
    live: bool = True              # show the live picture of `hwnd` (False: a title card, e.g. a background tab)
    minimized: bool = False
    payload: Any = field(default=None, repr=False)   # e.g. the UI-Automation element of a tab


def ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def lerp_rect(a: Rect, b: Rect, t: float) -> Rect:
    return tuple(x + (y - x) * t for x, y in zip(a, b))  # type: ignore[return-value]


def layout(aspects: list[float], width: float, height: float, gap: float = 28.0, label: float = 30.0,
           max_card: float = 0.62) -> list[Rect]:
    """Place cards (given their aspect ratios) in centred rows filling ``width`` x ``height`` as well as possible.

    Tries every row count and keeps the one with the largest cards; a single window is never shown huge
    (``max_card`` of the area). Each rect is the picture; ``label`` px under it are kept free for the title."""
    n = len(aspects)
    if n == 0 or width <= 0 or height <= 0:
        return []
    best: tuple[float, list[Rect]] | None = None
    for rows in range(1, n + 1):
        per_row = math.ceil(n / rows)
        groups = [aspects[i:i + per_row] for i in range(0, n, per_row)]
        row_h = (height - gap * (len(groups) + 1)) / len(groups) - label
        if row_h <= 10:
            break
        heights = []
        for group in groups:                          # each row: as tall as fits its total width
            total = sum(group)
            heights.append(min(row_h, (width - gap * (len(group) + 1)) / total, height * max_card))
        scale = min(heights)
        if best is None or scale > best[0]:
            rects: list[Rect] = []
            used = len(groups) * (scale + label) + gap * (len(groups) - 1)
            y = (height - used) / 2
            for group in groups:
                row_w = sum(a * scale for a in group) + gap * (len(group) - 1)
                x = (width - row_w) / 2
                for a in group:
                    rects.append((x, y, a * scale, scale))
                    x += a * scale + gap
                y += scale + label + gap
            best = (scale, rects)
    return best[1] if best else []


def fallback_source(target: Rect, screen_w: float, screen_h: float) -> Rect:
    """Where a minimised window (or a tab) comes from: a small rect at the bottom centre (the taskbar)."""
    w, h = target[2] * 0.25, target[3] * 0.25
    return (screen_w / 2 - w / 2, screen_h - h - 4, w, h)


def neighbour(rects: list[Rect], index: int, direction: str) -> int:
    """Keyboard navigation: the card to the left / right / above / below ``index``."""
    if not rects:
        return -1
    if index < 0:
        return 0
    cx, cy = rects[index][0] + rects[index][2] / 2, rects[index][1] + rects[index][3] / 2
    best, best_d = index, float("inf")
    for i, (x, y, w, h) in enumerate(rects):
        if i == index:
            continue
        dx, dy = x + w / 2 - cx, y + h / 2 - cy
        ok = {"left": dx < -1, "right": dx > 1, "up": dy < -1, "down": dy > 1}[direction]
        if not ok:
            continue
        along, across = (abs(dx), abs(dy)) if direction in ("left", "right") else (abs(dy), abs(dx))
        d = along + across * 2.5
        if d < best_d:
            best, best_d = i, d
    return best
