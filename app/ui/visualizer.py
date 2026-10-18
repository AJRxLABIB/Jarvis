"""The assistant's core: an animated reactor HUD, drawn entirely in code (original artwork).

Outside in:
* telemetry: four arcs (CPU, GPU, memory, battery) with leader lines to their read-outs
* a slowly turning tick bezel, and a gold segmented ring turning the other way
* a voice ring: a circular waveform that follows the microphone (listening) or pulses (speaking)
* a ring of ten coil segments that light up in sequence, and the glowing core

State changes colour and motion: IDLE breathes | LISTENING the voice ring follows you |
THINKING a radar sweep | EXECUTING fast spin | SPEAKING pulse waves | ERROR steady red.
The animation slows down when idle and stops completely while the window is hidden.
"""

from __future__ import annotations

import math
import time
from collections import deque

import numpy as np

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor, QConicalGradient, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.ui.styles.theme import GOLD, STATE_COLORS, TEXT
from app.ui.styles.tokens import COLOR

_ACTIVE_FPS, _IDLE_FPS = 50, 30  # smooth motion; each frame is cheap now (layers are cached)
_WAVE_POINTS = 120
MONO = ["Cascadia Mono", "Consolas", "Courier New"]

# (key, label, colour, centre angle in Qt degrees: 0 = 3 o'clock, counter-clockwise)
TELEMETRY = (
    ("cpu", "CPU", COLOR["accent"], 135.0),
    ("gpu", "GPU", GOLD, 45.0),
    ("ram", "MEMORY", COLOR["violet"], 225.0),
    ("battery", "BATTERY", COLOR["ok"], 315.0),
)
ARC_SPAN = 64.0


def _font(size: float, bold: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(MONO)
    font.setPointSizeF(size)
    font.setBold(bold)
    return font


def _alpha(color: QColor | str, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


class OrbWidget(QWidget):
    """The reactor. (Kept the old class name: the rest of the app and the tests use it.)"""

    def __init__(self, parent: QWidget | None = None, compact: bool = False) -> None:
        super().__init__(parent)
        self.compact = compact  # the mini orb: no telemetry read-outs or grid, fits in ~120 px
        self.setMinimumSize(32 if compact else 300, 32 if compact else 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._state = "IDLE"
        self._level = 0.0  # 0..1 microphone level
        self._wave: deque[float] = deque([0.0] * _WAVE_POINTS, maxlen=_WAVE_POINTS)
        self._telemetry: dict[str, tuple[float | None, str]] = {k: (None, "--") for k, *_ in TELEMETRY}
        self.hidden_readouts: set[str] = set()      # telemetry keys whose label/value text is not drawn
        self.hud_rings = True                        # False: no flat rings, gauges or tick bezel around the 3D core
        self._shown: dict[str, float] = {k: 0.0 for k, *_ in TELEMETRY}  # animated values
        self._start = time.monotonic()
        self.anim_clock: float | None = None   # set by the 3D core: its smooth, stall-free clock drives the HUD too
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)  # even frame spacing (Windows' coarse timers judder)
        self._timer.timeout.connect(self._frame)
        self._apply_rate()

    # ----------------------------------------------------------------- inputs

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        if state not in STATE_COLORS:
            state = "IDLE"
        if state != self._state:
            self._state = state
            self._apply_rate()
            self.update()

    def set_level(self, dbfs: float) -> None:
        """Microphone loudness in dBFS (-100..0) -> 0..1 with smoothing."""
        target = max(0.0, min(1.0, (dbfs + 60.0) / 50.0))
        self._level = self._level * 0.5 + target * 0.5

    def set_telemetry(self, key: str, value: float | None, text: str | None = None) -> None:
        """value 0..100 (None = not available) and the read-out, e.g. ("gpu", 31, "31% · 52°C")."""
        if key in self._telemetry:
            value = None if value is None else max(0.0, min(100.0, float(value)))
            self._telemetry[key] = (value, text if text is not None else ("--" if value is None else f"{value:.0f}%"))

    def _apply_rate(self) -> None:
        if not self.isVisible():
            self._timer.stop()
            return
        fps = _IDLE_FPS if self._state in ("IDLE", "ERROR") else _ACTIVE_FPS
        self._timer.start(int(1000 / fps))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_rate()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    @property
    def animating(self) -> bool:
        return self._timer.isActive()

    def anim_time(self) -> float:
        """Seconds of animation: the 3D core's clock when it drives this HUD (never jumps after a late frame)."""
        return self.anim_clock if self.anim_clock is not None else time.monotonic() - self._start

    def _frame(self) -> None:
        t = self.anim_time()
        if self._state == "LISTENING":
            sample = self._level
        elif self._state == "SPEAKING":
            sample = 0.35 + 0.35 * abs(math.sin(t * 6.3)) * abs(math.sin(t * 2.1 + 1))
        else:
            sample = 0.0
        self._wave.append(sample)
        self._level *= 0.92  # decays between microphone updates
        for key, (value, _) in self._telemetry.items():
            target = value or 0.0
            self._shown[key] += (target - self._shown[key]) * 0.15
        self.update()

    # ------------------------------------------------------------------ paint

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paint_full(p, self.width(), self.height())
        p.end()

    def paint_full(self, p: QPainter, w: int, h: int) -> None:
        """The painter-drawn core (also the fallback when the OpenGL core is unavailable)."""
        t = self.anim_time()
        c = QPointF(w / 2, h / 2)
        R = min(w, h) * (0.47 if self.compact else 0.41)
        color = QColor(STATE_COLORS[self._state])
        state = self._state
        speed = {"IDLE": 1.0, "LISTENING": 1.5, "THINKING": 2.5, "EXECUTING": 5.0, "SPEAKING": 1.6, "ERROR": 0.0,
                 "WAITING_FOR_PERMISSION": 0.8}[state]
        if not self.compact:
            self._paint_backdrop(p, c, R, color, t)
            self._paint_telemetry(p, c, R, w, h)
        self._paint_bezel(p, c, R, color, t, speed)
        self._paint_gold_ring(p, c, R, t, speed, state)
        self._paint_voice_ring(p, c, R, color, t, state)
        self._paint_coils(p, c, R, color, t, speed, state)
        if self.compact:
            self._paint_core(p, c, R, color, t, state)
        else:
            if not self.external_holo:
                self._paint_streaks(p, c, R, w, color, t)
            self._paint_plasma(p, c, R, color, t, state)
            if not self.external_holo:
                self._paint_side_waves(p, c, R, w, color, t)

    def paint_overlay(self, p: QPainter, w: int, h: int, tint: QColor, waves: bool = False) -> None:
        """The flat HUD drawn over the 3D core: telemetry arcs with read-outs, the turning tick bezel and the gold ring."""
        t = self.anim_time()
        c = QPointF(w / 2, h / 2)
        R = min(w, h) * 0.41
        speed = {"IDLE": 1.0, "LISTENING": 1.5, "THINKING": 2.5, "EXECUTING": 5.0, "SPEAKING": 1.6, "ERROR": 0.0,
                 "WAITING_FOR_PERMISSION": 0.8}[self._state]
        if waves:                                 # energy streaks and the audio waves left and right of the core
            self._paint_streaks(p, c, R, w, tint, t)
            self._paint_side_waves(p, c, R, w, tint, t)
        if not self.hud_rings:                    # the 3D core alone (CORE_HUD_RINGS=false)
            return
        self._paint_telemetry(p, c, R, w, h)
        self._paint_bezel(p, c, R, tint, t, speed)
        self._paint_gold_ring(p, c, R, t, speed, self._state)

    # ---- layers that don't change every frame are drawn once into pixmaps and copied (same look, far
    # less work per frame: the backdrop alone took ~3.7 ms of a ~8.6 ms frame).
    def _layer(self, key, size: tuple[int, int], draw) -> QPixmap:
        cache = self.__dict__.setdefault("_layers", {})
        pixmap = cache.get(key)
        if pixmap is None:
            dpr = self.devicePixelRatioF() or 1.0
            pixmap = QPixmap(max(1, int(size[0] * dpr)), max(1, int(size[1] * dpr)))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            draw(painter)
            painter.end()
            if len(cache) > 24:
                cache.clear()
            cache[key] = pixmap
        return pixmap

    def _paint_backdrop(self, p: QPainter, c: QPointF, R: float, color: QColor, t: float) -> None:
        w, h = self.width(), self.height()
        still = self._layer(("backdrop", w, h, round(R, 1), color.rgba()), (w, h),
                            lambda q: self._draw_backdrop_still(q, c, R, color))
        p.drawPixmap(0, 0, still)
        r = R * 1.12
        side = int(2 * r) + 2
        wedge = self._layer(("sweep", side, color.rgba()), (side, side),
                            lambda q: self._draw_sweep(q, QPointF(side / 2, side / 2), r, color))
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.translate(c)
        p.rotate((t * 25) % 360)  # = the conical gradient at -(t*25) degrees
        p.drawPixmap(QPointF(-side / 2, -side / 2), wedge)
        p.restore()

    def _draw_sweep(self, p: QPainter, c: QPointF, r: float, color: QColor) -> None:
        sweep = QConicalGradient(c, 0)
        sweep.setColorAt(0.0, _alpha(color, 0.10))
        sweep.setColorAt(0.08, QColor(0, 0, 0, 0))
        sweep.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(sweep)
        p.drawEllipse(c, r, r)

    def _draw_backdrop_still(self, p: QPainter, c: QPointF, R: float, color: QColor) -> None:
        glow = QRadialGradient(c, R * 1.25)
        glow.setColorAt(0.0, _alpha(color, 0.20))
        glow.setColorAt(0.5, _alpha(color, 0.05))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(c, R * 1.25, R * 1.25)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # faint radial grid
        p.setPen(QPen(_alpha(color, 0.07), 1))
        for i in range(12):
            a = math.radians(i * 30)
            p.drawLine(QPointF(c.x() + R * 0.30 * math.cos(a), c.y() + R * 0.30 * math.sin(a)),
                       QPointF(c.x() + R * 1.12 * math.cos(a), c.y() + R * 1.12 * math.sin(a)))
        for k in (0.34, 0.66, 1.12):
            p.drawEllipse(c, R * k, R * k)

    def _paint_telemetry(self, p: QPainter, c: QPointF, R: float, w: float, h: float) -> None:
        """Redrawn only when a value or read-out changes (the arcs glide for a moment after an update)."""
        key = ("telemetry", int(w), int(h), round(R, 1), tuple(self._telemetry.items()), tuple(sorted(self.hidden_readouts)),
               tuple(round(v, 1) for v in self._shown.values()))
        p.drawPixmap(0, 0, self._layer(key, (int(w), int(h)), lambda q: self._draw_telemetry(q, c, R, w, h)))

    def _draw_telemetry(self, p: QPainter, c: QPointF, R: float, w: float, h: float) -> None:
        r = R * 1.03
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        thick = max(4.0, R * 0.035)
        for key, label, hexcolor, centre in TELEMETRY:
            col = QColor(hexcolor)
            value, text = self._telemetry[key]
            start = centre - ARC_SPAN / 2
            track = QPen(_alpha(col, 0.14), thick)
            track.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(track)
            p.drawArc(rect, int(start * 16), int(ARC_SPAN * 16))
            # fill grows from the end nearest the horizontal axis
            shown = self._shown[key] / 100.0
            if value is not None and shown > 0.005:
                pen = QPen(col, thick)
                pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                p.setPen(pen)
                clockwise = centre in (45.0, 225.0)
                origin = start if not clockwise else start + ARC_SPAN
                span = ARC_SPAN * shown * (1 if not clockwise else -1)
                p.drawArc(rect, int(origin * 16), int(span * 16))
            # tick marks across the arc
            p.setPen(QPen(_alpha(col, 0.5), 1))
            for i in range(9):
                a = math.radians(start + ARC_SPAN * i / 8)
                r0, r1 = r + thick * 0.9, r + thick * (1.6 if i % 4 == 0 else 1.2)
                p.drawLine(QPointF(c.x() + r0 * math.cos(a), c.y() - r0 * math.sin(a)),
                           QPointF(c.x() + r1 * math.cos(a), c.y() - r1 * math.sin(a)))
            if key in self.hidden_readouts:
                continue
            # leader line and read-out
            a = math.radians(centre)
            ex, ey = math.cos(a), -math.sin(a)
            p1 = QPointF(c.x() + (r + thick * 2) * ex, c.y() + (r + thick * 2) * ey)
            p2 = QPointF(c.x() + R * 1.22 * ex, c.y() + R * 1.22 * ey)
            end_x = c.x() + ex * min(w / 2 - 6, R * 1.22 * abs(ex) + R * 0.28)
            p3 = QPointF(end_x, p2.y())
            p.setPen(QPen(_alpha(col, 0.55), 1))
            p.drawLine(p1, p2)
            p.drawLine(p2, p3)
            p.setBrush(col)
            p.drawEllipse(p1, 2.2, 2.2)
            p.setBrush(Qt.BrushStyle.NoBrush)
            box_w, box_h = max(90.0, R * 0.55), 36.0
            left = p3.x() - box_w if ex > 0 else p3.x()
            top = p3.y() - box_h - 2 if ey < 0 else p3.y() + 2
            box = QRectF(left, top, box_w, box_h)
            align = (Qt.AlignmentFlag.AlignRight if ex > 0 else Qt.AlignmentFlag.AlignLeft)
            p.setFont(_font(7.5, bold=True))
            p.setPen(_alpha(col, 0.95))
            p.drawText(QRectF(box.left(), box.top(), box.width(), 14), align | Qt.AlignmentFlag.AlignVCenter, label)
            p.setFont(_font(10.5, bold=True))
            p.setPen(QColor(TEXT) if value is not None else _alpha(TEXT, 0.4))
            p.drawText(QRectF(box.left(), box.top() + 14, box.width(), 20), align | Qt.AlignmentFlag.AlignVCenter, text)

    def _paint_bezel(self, p: QPainter, c: QPointF, R: float, color: QColor, t: float, speed: float) -> None:
        # the 120 ticks are drawn once into a pixmap per size/colour and then just turned (1 draw instead of 120)
        tint = QColor(color)
        key_color = (tint.red() // 8, tint.green() // 8, tint.blue() // 8)
        side = int(R * 2 * 0.95) + 4
        ratio = 2.0
        ring = self._layer(("bezel", side, key_color), (int(side * ratio), int(side * ratio)),
                           lambda q: self._draw_ticks(q, side * ratio / 2, R * ratio, tint))
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.translate(c)
        p.rotate((t * 5 * speed) % 360)
        p.drawPixmap(QRectF(-side / 2, -side / 2, side, side), ring, QRectF(ring.rect()))
        p.restore()
        p.setPen(QPen(_alpha(color, 0.45), 1))
        p.drawEllipse(c, R * 0.945, R * 0.945)

    def _draw_ticks(self, p: QPainter, centre: float, R: float, color: QColor) -> None:
        p.translate(centre, centre)
        for i in range(120):
            long = i % 10 == 0
            p.setPen(QPen(_alpha(color, 0.8 if long else 0.28), 4.0 if long else 2.0))
            p.drawLine(QPointF(R * (0.86 if long else 0.895), 0), QPointF(R * 0.93, 0))
            p.rotate(3)

    def _paint_bezel_lines(self, p: QPainter, c: QPointF, R: float, color: QColor, t: float, speed: float) -> None:
        p.save()
        p.translate(c)
        p.rotate((t * 5 * speed) % 360)
        for i in range(120):
            long = i % 10 == 0
            p.setPen(QPen(_alpha(color, 0.8 if long else 0.28), 2.0 if long else 1.0))
            p.drawLine(QPointF(R * (0.86 if long else 0.895), 0), QPointF(R * 0.93, 0))
            p.rotate(3)
        p.restore()
        p.setPen(QPen(_alpha(color, 0.45), 1))
        p.drawEllipse(c, R * 0.945, R * 0.945)

    def _paint_gold_ring(self, p: QPainter, c: QPointF, R: float, t: float, speed: float, state: str) -> None:
        gold = QColor(GOLD) if state != "ERROR" else QColor(STATE_COLORS["ERROR"])
        r = R * 0.80
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        pen = QPen(_alpha(gold, 0.85), max(3.0, R * 0.028))
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        segments = 16 if state == "EXECUTING" else 8
        span = 360 / segments
        offset = -t * 14 * speed
        for i in range(segments):
            p.drawArc(rect, int((offset + i * span) * 16), int(span * 0.7 * 16))
        # small bright markers riding on the ring
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_alpha(gold, 0.95))
        for i in range(4):
            a = math.radians(offset + i * 90 + span * 0.85)
            p.drawEllipse(QPointF(c.x() + r * math.cos(a), c.y() - r * math.sin(a)), 2.4, 2.4)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_alpha(gold, 0.25), 1, Qt.PenStyle.DashLine))
        p.drawEllipse(c, R * 0.74, R * 0.74)

    def _paint_voice_ring(self, p: QPainter, c: QPointF, R: float, color: QColor, t: float, state: str) -> None:
        base = R * 0.64
        if state == "THINKING":  # radar sweep
            sweep = QConicalGradient(c, -(t * 220) % 360)
            sweep.setColorAt(0.0, _alpha(color, 0.55))
            sweep.setColorAt(0.18, QColor(0, 0, 0, 0))
            sweep.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(sweep)
            p.drawEllipse(c, base * 1.05, base * 1.05)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(3):
                r = base * (0.88 + i * 0.07)
                rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
                pen = QPen(_alpha(color, 0.85 - i * 0.2), 2.2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                start = (t * (150 + i * 60) * (1 if i % 2 == 0 else -1) + i * 120) % 360
                p.drawArc(rect, int(start * 16), 70 * 16)
            return
        if state == "EXECUTING":
            rect = QRectF(c.x() - base, c.y() - base, 2 * base, 2 * base)
            p.setPen(QPen(_alpha(color, 0.9), 3))
            for i in range(24):
                p.drawArc(rect, int(((i * 15 + t * 300) % 360) * 16), 6 * 16)
            return
        # circular waveform (flat when quiet)
        wave = list(self._wave)
        path = QPainterPath()
        n = len(wave)
        for i in range(n + 1):
            k = i % n
            a = 2 * math.pi * k / n - math.pi / 2
            ripple = math.sin(k * 0.9 + t * 8) * 0.5 + 0.5
            amp = wave[(k * 7) % n] * (0.45 + 0.55 * ripple)
            r = base * (1.0 + 0.16 * amp)
            point = QPointF(c.x() + r * math.cos(a), c.y() + r * math.sin(a))
            path.moveTo(point) if i == 0 else path.lineTo(point)
        active = max(wave[-10:]) if wave else 0.0
        p.setPen(QPen(_alpha(color, 0.35 + 0.6 * min(1.0, active * 2)), 1.6))
        p.drawPath(path)
        if state == "SPEAKING":
            for i in range(3):
                phase = (t * 0.8 + i / 3) % 1.0
                p.setPen(QPen(_alpha(color, 0.6 * (1 - phase)), 1.6))
                r = base * (0.62 + phase * 0.5)
                p.drawEllipse(c, r, r)

    def _paint_coils(self, p: QPainter, c: QPointF, R: float, color: QColor, t: float, speed: float,
                     state: str) -> None:
        inner, outer = (R * 0.40, R * 0.54) if self.compact else (R * 0.71, R * 0.77)
        count = 10
        span = 360 / count
        lit = (t * 2.2 * max(0.5, speed)) % count
        paths = self._coil_paths(inner, outer, count, span)  # built once per size, then just turned
        p.save()
        p.translate(c)
        p.rotate((t * 4 * speed) % 360)
        edge = QPen(_alpha(color, 0.9), 1)
        for i, path in enumerate(paths):
            distance = min((i - lit) % count, (lit - i) % count)
            glow = 0.25 + 0.55 * max(0.0, 1.0 - distance / 2.5) if state != "ERROR" else 0.5
            if state == "IDLE":
                glow = 0.30 + 0.25 * (0.5 + 0.5 * math.sin(t * 1.4 + i * 0.63))
            p.setPen(edge)
            p.setBrush(_alpha(color, glow * 0.55))
            p.drawPath(path)
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_alpha(color, 0.6), 1.2))
        p.drawEllipse(c, outer + 3, outer + 3)
        p.drawEllipse(c, inner - 3, inner - 3)

    def _coil_paths(self, inner: float, outer: float, count: int, span: float) -> list[QPainterPath]:
        key = (round(inner, 2), round(outer, 2), count)
        cached = self.__dict__.get("_coils")
        if cached is not None and cached[0] == key:
            return cached[1]
        paths = []
        for i in range(count):
            a0 = math.radians(i * span + 3)
            a1 = math.radians((i + 1) * span - 3)
            path = QPainterPath(QPointF(inner * math.cos(a0), inner * math.sin(a0)))
            path.lineTo(outer * math.cos(a0), outer * math.sin(a0))
            path.arcTo(QRectF(-outer, -outer, 2 * outer, 2 * outer), -math.degrees(a0),
                       -(math.degrees(a1) - math.degrees(a0)))
            path.lineTo(inner * math.cos(a1), inner * math.sin(a1))
            path.arcTo(QRectF(-inner, -inner, 2 * inner, 2 * inner), -math.degrees(a1),
                       math.degrees(a1) - math.degrees(a0))
            path.closeSubpath()
            paths.append(path)
        self._coils = (key, paths)
        return paths

    def _globe_geometry(self):
        """Fixed shapes of the plasma globe (built once): dust points inside a sphere and the orbit loops."""
        cached = self.__dict__.get("_globe")
        if cached is not None:
            return cached
        rng = np.random.default_rng(7)
        n = 300
        i = np.arange(n) + 0.5
        phi = np.arccos(1 - 2 * i / n)
        theta = math.pi * (1 + 5 ** 0.5) * i
        dust = np.stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], axis=1)
        dust *= np.sqrt(rng.uniform(0.18, 1.0, (n, 1)))
        angle = np.linspace(0, 2 * math.pi, 85)
        circle = np.stack([np.cos(angle), np.sin(angle), np.zeros_like(angle)], axis=1)
        # (radius vs the sphere, start angles about x / y / z, turn rates rad/s about x / y / z, colour)
        loops = [(1.00, 0.2, 0.0, 0.0, 0.31, 0.11, 0.05, 0), (1.12, 1.0, 0.5, 0.3, -0.17, 0.27, 0.09, 1),
                 (1.28, 2.1, 1.2, 0.7, 0.13, -0.21, 0.15, 2), (0.92, 0.7, 2.0, 1.1, -0.29, 0.14, -0.12, 0),
                 (1.20, 1.6, 0.2, 2.2, 0.22, 0.19, 0.08, 3), (1.06, 2.8, 1.8, 0.4, -0.11, -0.25, 0.17, 1),
                 (1.36, 0.4, 2.6, 1.9, 0.09, 0.23, -0.14, 2), (0.84, 1.3, 0.9, 2.9, 0.26, -0.15, 0.2, 3),
                 (1.16, 2.4, 2.3, 0.6, -0.20, 0.12, 0.11, 0), (1.30, 0.9, 1.5, 1.5, 0.15, -0.18, -0.07, 1)]
        self._globe = (dust, circle, loops)
        return self._globe

    @staticmethod
    def _rotation(ax: float, ay: float, az: float):
        cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
        rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
        ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
        rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
        return rx @ ry @ rz

    def _paint_plasma(self, p: QPainter, c: QPointF, R: float, color: QColor, t: float, state: str) -> None:
        """The plasma globe: a glass sphere full of drifting plasma dust, ringed by glowing orbit loops that tumble in
        3D (real rotations, front half bright, back half dim). It spins faster when Jarvis is busy, breathes when
        idle, pulses while speaking and swells with your voice while listening."""
        sr = R * 0.50
        if state == "IDLE":
            sr *= 1.0 + 0.02 * math.sin(t * 1.3)
        elif state == "SPEAKING":
            sr *= 1.0 + 0.05 * abs(math.sin(t * 7))
        elif state == "LISTENING":
            sr *= 1.0 + 0.08 * min(1.0, self._level * 1.5)
        spin = {"IDLE": 1.0, "LISTENING": 1.4, "THINKING": 2.4, "EXECUTING": 3.4, "SPEAKING": 1.8, "ERROR": 0.0,
                "WAITING_FOR_PERMISSION": 0.7}.get(state, 1.0)
        halo = QRadialGradient(c, sr * 2.0)
        halo.setColorAt(0.0, _alpha(color, 0.60))
        halo.setColorAt(0.45, _alpha(color, 0.20))
        halo.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, sr * 2.0, sr * 2.0)
        ball = QRadialGradient(c + QPointF(-sr * 0.2, -sr * 0.25), sr * 1.15)
        ball.setColorAt(0.0, _alpha(color.lighter(170), 0.95))
        ball.setColorAt(0.45, _alpha(QColor("#6d5dfc"), 0.55))
        ball.setColorAt(0.8, _alpha(color.darker(150), 0.75))
        ball.setColorAt(1.0, QColor(3, 12, 28, 235))
        p.setBrush(ball)
        p.drawEllipse(c, sr, sr)
        dust, circle, loops = self._globe_geometry()
        tones = (QColor(COLOR["cyan"]), QColor(COLOR["violet"]), QColor(COLOR["gold"]), QColor("#f472b6"))
        # soft plasma ribbons swirling inside the sphere
        for k in range(4):
            tone = tones[(k + 1) % 4]
            ribbon = QPainterPath()
            for j in range(41):
                f = j / 40
                ang = f * 2 * math.pi * 1.5 + t * (0.7 + 0.2 * k) * spin + k * 1.7
                rad = sr * (0.25 + 0.7 * abs(math.sin(f * math.pi)))
                pt = QPointF(c.x() + rad * math.cos(ang), c.y() + rad * 0.62 * math.sin(ang + k))
                if j == 0:
                    ribbon.moveTo(pt)
                else:
                    ribbon.lineTo(pt)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_alpha(tone, 0.16), sr * 0.16))
            p.drawPath(ribbon)
        # plasma dust: a cloud turning inside the sphere
        turn = self._rotation(0.35, t * 0.55 * spin, 0.1)
        pts = dust @ turn.T
        pts = pts.tolist()
        front = [QPointF(c.x() + sr * x, c.y() + sr * y) for x, y, z in pts if z >= 0]
        back = [QPointF(c.x() + sr * x, c.y() + sr * y) for x, y, z in pts if z < 0]
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_alpha(QColor(COLOR["text_strong"]), 0.30), 1.6))
        p.drawPoints(QPolygonF(back))
        p.setPen(QPen(_alpha(color.lighter(180), 0.95), 2.2))
        p.drawPoints(QPolygonF(front))
        # orbit loops, tumbling in 3D
        for radius, a0, b0, g0, ra, rb, rg, tone_index in loops:
            tone = tones[tone_index]
            ring = (circle * (sr * radius)) @ self._rotation(a0 + ra * t * spin, b0 + rb * t * spin,
                                                              g0 + rg * t * spin).T
            near, far = QPainterPath(), QPainterPath()
            last_near = last_far = False
            for x, y, z in ring.tolist():
                pt = QPointF(c.x() + x, c.y() + y)
                if z >= 0:
                    if last_near:
                        near.lineTo(pt)
                    else:
                        near.moveTo(pt)
                    last_near, last_far = True, False
                else:
                    if last_far:
                        far.lineTo(pt)
                    else:
                        far.moveTo(pt)
                    last_far, last_near = True, False
            p.setPen(QPen(_alpha(tone, 0.28), 1.1))
            p.drawPath(far)
            p.setPen(QPen(_alpha(tone, 0.20), 5.0))
            p.drawPath(near)                                  # soft glow under the bright line
            p.setPen(QPen(_alpha(tone.lighter(130), 0.95), 1.7))
            p.drawPath(near)
        glow = QRadialGradient(c, sr * 0.6)
        glow.setColorAt(0.0, QColor(255, 255, 255, 240))
        glow.setColorAt(0.35, _alpha(color.lighter(150), 0.55))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(c, sr * 0.6, sr * 0.6)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_alpha(QColor(COLOR["text_strong"]), 0.5), 1.2))
        p.drawEllipse(c, sr, sr)

    @staticmethod
    def _circle_path(radius: float) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), radius, radius)
        return path

    external_holo = False      # True: the window background draws the streaks and waves (across the whole window)

    def paint_holo(self, p: QPainter, c: QPointF, R: float, w: float, color: QColor) -> None:
        """The streaks and waves for the window background: from the core out to both window edges."""
        t = self.anim_time()
        self._paint_streaks(p, c, R, w, color, t, full=True)
        self._paint_side_waves(p, c, R, w, color, t, full=True)

    def _paint_streaks(self, p: QPainter, c: QPointF, R: float, w: float, color: QColor, t: float,
                       full: bool = False) -> None:
        """Curved energy lines flowing from the globe out to both sides of the panel (decoration, always moving)."""
        tones = (color, QColor(COLOR["violet"]), QColor(COLOR["gold"]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for side in (-1, 1):
            for k in range(6):
                y0 = c.y() + (k - 2.5) * R * 0.09
                far = (c.x() if side < 0 else w - c.x()) if full else min(w / 2 - 3, R * 1.55)
                end = QPointF(c.x() + side * far, c.y() + (k - 2.5) * R * 0.20 + math.sin(t * 0.6 + k) * R * 0.05)
                ctrl = QPointF(c.x() + side * R * 1.05, c.y() + math.sin(t * 0.8 + k * 1.3 + side) * R * 0.30)
                path = QPainterPath(QPointF(c.x() + side * R * 0.40, y0))
                path.quadTo(ctrl, end)
                tone = tones[k % 3]
                p.setPen(QPen(_alpha(tone, 0.10), 4.0))
                p.drawPath(path)
                p.setPen(QPen(_alpha(tone, 0.42), 1.0))
                p.drawPath(path)

    def _paint_side_waves(self, p: QPainter, c: QPointF, R: float, w: float, color: QColor, t: float,
                          full: bool = False) -> None:
        """Audio waves left and right of the core. Their height is the real microphone / speech level (a flat dotted
        line when nothing is heard); the newest sound is nearest the core and travels outward."""
        reach = (min(c.x(), w - c.x()) - R * 0.62 - 8) if full else min(c.x() - R * 0.62 - 8, w * 0.36)
        if reach < 20:
            return
        history = list(self._wave)
        n = 48
        for side in (-1, 1):
            for layer, (tone, width) in enumerate(((color, 1.4), (QColor(COLOR["violet"]), 1.0))):
                path = QPainterPath()
                for i in range(n + 1):
                    f = i / n
                    level = max(0.12, history[-1 - int(f * (len(history) - 1))] if history else 0.0)  # 0.12 = idle shimmer
                    wobble = math.sin(f * 17 - t * 9 + layer * 1.7)
                    y = c.y() + wobble * level * R * 0.34 * (1 - f * 0.55)
                    x = c.x() + side * (R * 0.62 + 8 + f * reach)
                    if i == 0:
                        path.moveTo(QPointF(x, y))
                    else:
                        path.lineTo(QPointF(x, y))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(_alpha(tone, 0.8 - 0.25 * layer), width))
                p.drawPath(path)

    def _paint_core(self, p: QPainter, c: QPointF, R: float, color: QColor, t: float, state: str) -> None:
        core_r = R * 0.28
        if state == "IDLE":
            core_r *= 1.0 + 0.03 * math.sin(t * 1.3)
        elif state == "SPEAKING":
            core_r *= 1.0 + 0.06 * abs(math.sin(t * 7))
        elif state == "LISTENING":
            core_r *= 1.0 + 0.10 * min(1.0, self._level * 1.5)
        halo = QRadialGradient(c, core_r * 1.9)
        halo.setColorAt(0.0, _alpha(color, 0.65))
        halo.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, core_r * 1.9, core_r * 1.9)
        core = QRadialGradient(c, core_r)
        core.setColorAt(0.0, QColor(255, 255, 255, 255))
        core.setColorAt(0.35, color.lighter(150))
        core.setColorAt(0.8, color)
        core.setColorAt(1.0, color.darker(250))
        p.setBrush(core)
        p.drawEllipse(c, core_r, core_r)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_alpha("#ffffff", 0.5), 1))
        p.drawEllipse(c, core_r * 0.62, core_r * 0.62)
        # rotating inner hexagon
        p.setPen(QPen(_alpha("#ffffff", 0.35), 1))
        hexagon = QPainterPath()
        for i in range(7):
            a = math.radians(i * 60 + t * 20)
            point = QPointF(c.x() + core_r * 0.82 * math.cos(a), c.y() + core_r * 0.82 * math.sin(a))
            hexagon.moveTo(point) if i == 0 else hexagon.lineTo(point)
        p.drawPath(hexagon)
