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

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QConicalGradient, QFont, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.ui.styles.theme import GOLD, STATE_COLORS, TEXT

_ACTIVE_FPS, _IDLE_FPS = 50, 30  # smooth motion; each frame is cheap now (layers are cached)
_WAVE_POINTS = 120
MONO = ["Cascadia Mono", "Consolas", "Courier New"]

# (key, label, colour, centre angle in Qt degrees: 0 = 3 o'clock, counter-clockwise)
TELEMETRY = (
    ("cpu", "CPU", "#38bdf8", 135.0),
    ("gpu", "GPU", GOLD, 45.0),
    ("ram", "MEMORY", "#a78bfa", 225.0),
    ("battery", "BATTERY", "#34d399", 315.0),
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
        self._shown: dict[str, float] = {k: 0.0 for k, *_ in TELEMETRY}  # animated values
        self._start = time.monotonic()
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

    def _frame(self) -> None:
        t = time.monotonic() - self._start
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
        t = time.monotonic() - self._start
        w, h = self.width(), self.height()
        c = QPointF(w / 2, h / 2)
        R = min(w, h) * (0.47 if self.compact else 0.40)
        color = QColor(STATE_COLORS[self._state])
        state = self._state
        speed = {"IDLE": 1.0, "LISTENING": 1.5, "THINKING": 2.5, "EXECUTING": 5.0, "SPEAKING": 1.6, "ERROR": 0.0,
                 "WAITING_FOR_PERMISSION": 0.8}[state]

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.compact:
            self._paint_backdrop(p, c, R, color, t)
            self._paint_telemetry(p, c, R, w, h)
        self._paint_bezel(p, c, R, color, t, speed)
        self._paint_gold_ring(p, c, R, t, speed, state)
        self._paint_voice_ring(p, c, R, color, t, state)
        self._paint_coils(p, c, R, color, t, speed, state)
        self._paint_core(p, c, R, color, t, state)
        p.end()

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
        key = ("telemetry", int(w), int(h), round(R, 1), tuple(self._telemetry.items()),
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
        inner, outer = R * 0.40, R * 0.54
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
