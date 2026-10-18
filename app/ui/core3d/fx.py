"""Overlay effects drawn over the 3D backdrop with QPainter:

* **data pulses** - when something happens (an action runs, a reply arrives, an approval is needed, the status changes)
  a bright comet travels from the core to the panel that shows it, with a fading trail (CORE_PULSES);
* **spark trails** - a few sparks circle the core on tilted orbits, each leaving a fading trail (CORE_TRAILS).

The geometry is plain math (tested without a display); ``paint`` does the drawing. Reduced motion turns both off.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PULSE_SECONDS = 0.85
MAX_PULSES = 12
TRAIL_POINTS = 10
SPARKS = 6


@dataclass
class Pulse:
    target: object            # a QWidget (or anything with a rect we can map); resolved when drawn
    born: float
    color: tuple[float, float, float]


def ease(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def nearest_point(rect: tuple[float, float, float, float], point: tuple[float, float]) -> tuple[float, float]:
    """The point of ``rect`` (x, y, w, h) closest to ``point`` (the end of a pulse's path)."""
    x, y, w, h = rect
    px, py = point
    return (min(max(px, x), x + w), min(max(py, y), y + h))


def comet(start, end, progress: float, points: int = TRAIL_POINTS, length: float = 0.22):
    """Head and trail positions along start->end at ``progress`` (0..1): [(x, y, alpha, size)], head first."""
    out = []
    for i in range(points):
        t = ease(max(0.0, min(1.0, progress - i * length / points)))
        if progress - i * length / points < 0:
            break
        x = start[0] + (end[0] - start[0]) * t
        y = start[1] + (end[1] - start[1]) * t
        fade = 1.0 - i / points
        out.append((x, y, fade * (1.0 - max(0.0, progress - 0.8) / 0.2), 1.0 + 3.0 * fade))
    return out


def spark_trails(center, radius: float, clock: float, sparks: int = SPARKS, points: int = TRAIL_POINTS):
    """Each spark's trail on its own tilted ellipse around the core: list of [(x, y, alpha, size)] (head first)."""
    cx, cy = center
    out = []
    for k in range(sparks):
        tilt = k * math.pi / sparks + 0.3
        speed = 0.55 + 0.12 * (k % 3)
        rx, ry = radius * (1.08 + 0.05 * (k % 2)), radius * (0.36 + 0.08 * (k % 3))
        trail = []
        for i in range(points):
            a = clock * speed * (1 if k % 2 else -1) + k * 1.7 - i * 0.06 * (1 if k % 2 else -1)
            ex, ey = rx * math.cos(a), ry * math.sin(a)
            x = cx + ex * math.cos(tilt) - ey * math.sin(tilt)
            y = cy + ex * math.sin(tilt) + ey * math.cos(tilt)
            front = math.sin(a) > -0.2                   # behind the globe: dimmer
            fade = (1.0 - i / points) * (1.0 if front else 0.35)
            trail.append((x, y, fade, 0.8 + 2.2 * (1.0 - i / points)))
        out.append(trail)
    return out


class Effects:
    def __init__(self) -> None:
        self.pulses: list[Pulse] = []
        self.pulses_on = True
        self.trails_on = True

    def pulse(self, target, now: float, color=(0.3, 0.9, 1.0)) -> None:
        if not self.pulses_on or target is None:
            return
        self.pulses.append(Pulse(target, now, color))
        del self.pulses[:-MAX_PULSES]

    def live(self, now: float) -> list[tuple[Pulse, float]]:
        self.pulses = [p for p in self.pulses if now - p.born < PULSE_SECONDS]
        return [(p, (now - p.born) / PULSE_SECONDS) for p in self.pulses]

    @property
    def active(self) -> bool:
        return bool(self.pulses) or self.trails_on

    def paint(self, painter, center, radius: float, now: float, clock: float, rect_of, reduced_motion: bool) -> None:
        """``rect_of(target)`` -> (x, y, w, h) in this widget's coordinates, or None when it isn't visible."""
        if reduced_motion:
            self.pulses.clear()
            return
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QColor

        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setCompositionMode(painter.CompositionMode.CompositionMode_Plus)
        if self.trails_on:
            for trail in spark_trails(center, radius, clock):
                for x, y, alpha, size in trail:
                    painter.setBrush(QColor.fromRgbF(0.55, 0.95, 1.0, max(0.0, min(1.0, alpha * 0.55))))
                    painter.drawEllipse(QPointF(x, y), size, size)
        for pulse, progress in self.live(now):
            try:
                rect = rect_of(pulse.target)
            except RuntimeError:          # the panel was deleted
                rect = None
            if rect is None:
                continue
            end = nearest_point(rect, center)
            r, g, b = pulse.color
            for x, y, alpha, size in comet(center, end, progress):
                painter.setBrush(QColor.fromRgbF(r, g, b, max(0.0, min(1.0, alpha))))
                painter.drawEllipse(QPointF(x, y), size, size)
        painter.restore()
