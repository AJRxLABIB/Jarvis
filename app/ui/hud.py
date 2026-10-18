"""HUD building blocks, all drawn in code: the hex-grid backdrop, framed panels, segmented meters,
ring gauges and history graphs."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt
import math

from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.ui.styles.theme import ACCENT, BG, DIM, GOLD, PANEL_EDGE, TEXT

MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Courier New"]


def mono_font(size: float, bold: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setPointSizeF(size)
    font.setBold(bold)
    return font


class HexBackground(QWidget):
    """The window backdrop: deep blue gradient, a faint hexagon grid and a vignette.
    Rendered once per size into a pixmap, so it costs nothing while the reactor animates."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cache: QPixmap | None = None

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._cache = None
        super().resizeEvent(event)

    def _render(self) -> QPixmap:
        pixmap = QPixmap(self.size())
        pixmap.fill(QColor(BG))
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        base = QLinearGradient(0, 0, 0, h)
        base.setColorAt(0, QColor("#030a16"))
        base.setColorAt(1, QColor("#01040a"))
        p.fillRect(self.rect(), base)
        size = 22.0
        dx, dy = size * math.sqrt(3), size * 1.5
        p.setPen(QPen(QColor(56, 189, 248, 14), 1))
        row = 0
        y = 0.0
        while y < h + size:
            x = (dx / 2 if row % 2 else 0.0) - dx
            while x < w + dx:
                hexagon = QPainterPath()
                for i in range(7):
                    a = math.radians(60 * i + 30)
                    point = QPointF(x + size * math.cos(a), y + size * math.sin(a))
                    hexagon.moveTo(point) if i == 0 else hexagon.lineTo(point)
                p.drawPath(hexagon)
                x += dx
            y += dy
            row += 1
        glow = QRadialGradient(QPointF(w / 2, h * 0.52), max(w, h) * 0.55)
        glow.setColorAt(0, QColor(56, 189, 248, 26))
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), glow)
        vignette = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * 0.75)
        vignette.setColorAt(0.6, QColor(0, 0, 0, 0))
        vignette.setColorAt(1.0, QColor(0, 0, 0, 170))
        p.fillRect(self.rect(), vignette)
        p.end()
        return pixmap

    # ---- motion: a slow scan beam, hexagons that light up now and then, a glow in the state's colour.
    # Only the strips that change are repainted (never the whole window), at most ~30 times a second.
    SCAN_SECONDS = 9.0
    BEAM = 70.0
    PULSES = 5
    MOTES = 36
    STATE_TINTS = {"IDLE": (56, 189, 248), "LISTENING": (52, 211, 153), "THINKING": (245, 182, 66),
                   "SPEAKING": (34, 211, 238), "ERROR": (248, 113, 113),
                   "WAITING_FOR_PERMISSION": (245, 182, 66)}

    def start_motion(self) -> None:
        from app.ui import motion

        if not motion.ENABLED or getattr(self, "_moving", False):
            return
        import time as _time

        self._moving = True
        self._t0 = _time.monotonic()
        self._beam_y: float | None = None
        self._pulses: list[tuple[float, float, float]] = []  # (x, y, started)
        import random

        rng = random.Random(3)
        # drifting motes: [x, y, vx, vy, phase] in pixels / pixels per second
        self._motes = [[rng.uniform(0, max(1, self.width())), rng.uniform(0, max(1, self.height())),
                        rng.uniform(-9, 9), rng.uniform(-14, -3), rng.uniform(0, 6.3)] for _ in range(self.MOTES)]
        self._tint = self.STATE_TINTS["IDLE"]
        motion.CLOCK.join(self._tick)

    def set_state(self, state: str) -> None:
        self._tint = self.STATE_TINTS.get(state, self.STATE_TINTS["IDLE"])
        if getattr(self, "_moving", False) and self._beam_y is not None:
            self.update(0, int(self._beam_y - self.BEAM), self.width(), int(self.BEAM * 2))

    def _tick(self, now: float) -> bool:
        from PySide6.QtCore import QRect

        if not self.isVisible() or self.window().isMinimized():
            return True  # keep the clock, draw nothing
        if now - getattr(self, "_last_frame", 0.0) < 1 / 15:
            return True  # the backdrop moves slowly; the holographic band is paced by the core (holo_frame)
        self._last_frame = now
        if now - getattr(self, "_covered_at", -10.0) > 1.0:
            self._covered = self._covered_region()
            self._covered_at = now
        real_update = self._update_gaps
        import random

        h, w = self.height(), self.width()
        phase = ((now - self._t0) % self.SCAN_SECONDS) / self.SCAN_SECONDS
        old = self._beam_y
        self._beam_y = -self.BEAM + (h + 2 * self.BEAM) * phase
        top = min(old if old is not None else self._beam_y, self._beam_y) - self.BEAM
        bottom = max(old if old is not None else self._beam_y, self._beam_y) + self.BEAM
        if old is not None and abs(self._beam_y - old) > h / 2:  # wrapped round: repaint both ends
            real_update(QRect(0, int(old - self.BEAM), w, int(self.BEAM * 2 + 2)))
            top, bottom = self._beam_y - self.BEAM, self._beam_y + self.BEAM
        real_update(QRect(0, int(top), w, int(bottom - top) + 2))
        self._pulses = [pulse for pulse in self._pulses if now - pulse[2] < 2.4]
        if len(self._pulses) < self.PULSES and random.random() < 0.04:
            self._pulses.append((random.uniform(0, w), random.uniform(0, h), now))
        for x, y, _ in self._pulses:
            real_update(QRect(int(x - 30), int(y - 30), 60, 60))
        if getattr(self, "_motes_for", None) != (w, h):  # first frame at the real size (or resized): spread out
            self._motes_for = (w, h)
            for k, mote in enumerate(self._motes):
                mote[0] = (k * 0.618034 % 1.0) * w
                mote[1] = (k * 0.381966 * 3.7 % 1.0) * h
            self.update()  # once, at the new size
        dt = min(0.2, now - getattr(self, "_mote_at", now))
        self._mote_at = now
        for mote in self._motes:
            real_update(QRect(int(mote[0] - 6), int(mote[1] - 6), 12, 12))
            mote[0] = (mote[0] + mote[2] * dt) % max(1, w)
            mote[1] = (mote[1] + mote[3] * dt) % max(1, h)
            real_update(QRect(int(mote[0] - 6), int(mote[1] - 6), 12, 12))
        if not self.holo_paced:
            band = self._holo_band()
            if band is not None:              # no core to pace it: repaint the band with the backdrop
                real_update(band)
        return True

    holo_paced = False      # True once the 3D core drives holo_frame()
    HOLO_FPS = 40

    def holo_frame(self) -> None:
        """Called after each core frame: repaints the holographic band at up to HOLO_FPS, in step with the core."""
        import time as _time

        self.holo_paced = True
        now = _time.perf_counter()
        if now - getattr(self, "_holo_at", 0.0) < 1.0 / self.HOLO_FPS - 0.004:
            return
        self._holo_at = now
        band = self._holo_band()
        if band is not None and self.isVisible():
            if getattr(self, "_covered", None) is None:
                self._covered = self._covered_region()
            self._update_gaps(band)

    # ---- the holographic layer: energy streaks and audio waves from the core across the whole window
    holo_source = None   # callable -> (centre QPointF in this widget, radius, colour QColor, painter-owner) or None

    def _holo(self):
        source = self.holo_source
        if source is None:
            return None
        try:
            return source()
        except RuntimeError:                  # the core widget is being destroyed
            return None

    def _holo_band(self):
        from PySide6.QtCore import QRect

        holo = self._holo()
        if holo is None:
            return None
        c, R = holo[0], holo[1]
        return QRect(0, int(c.y() - R * 0.75), self.width(), int(R * 1.5))

    def _covered_region(self):
        """Where the panels are: the background shows only in the gaps, so only the gaps are repainted
        (repainting under a panel makes Qt repaint the whole panel - that was the lag)."""
        from PySide6.QtGui import QRegion
        from PySide6.QtWidgets import QFrame

        covered = QRegion()
        for child in self.findChildren(QFrame):
            if not child.isVisible() or child.width() < 40 or child.height() < 20:
                continue
            name = type(child).__name__
            if getattr(child, "frameless", False):
                continue                      # a frameless panel shows the background (and its animation) through
            if name in ("DashPanel", "StatusCard", "HudPanel") or child.objectName() == "dashHeader":
                top_left = child.mapTo(self, child.rect().topLeft())
                covered = covered.united(QRegion(top_left.x(), top_left.y(), child.width(), child.height()))
        return covered

    def _update_gaps(self, rect) -> None:
        from PySide6.QtGui import QRegion

        region = QRegion(rect)
        covered = getattr(self, "_covered", None)
        if covered is not None and not covered.isEmpty():
            region = region.subtracted(covered)
        if not region.isEmpty():
            self.update(region)

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._cache is None or self._cache.size() != self.size():
            self._cache = self._render()
        p = QPainter(self)
        clip = event.rect()
        p.drawPixmap(clip, self._cache, clip)
        if getattr(self, "_moving", False):
            self._paint_motion(p)
        holo = self._holo()
        if holo is not None:
            c, R, color, owner = holo
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            owner.paint_holo(p, c, R, self.width(), color)
        p.end()

    def _paint_motion(self, p: QPainter) -> None:
        import time as _time

        now = _time.monotonic()
        r, g, b = self._tint
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._beam_y is not None:
            beam = QLinearGradient(0, self._beam_y - self.BEAM, 0, self._beam_y + self.BEAM)
            beam.setColorAt(0.0, QColor(r, g, b, 0))
            beam.setColorAt(0.5, QColor(r, g, b, 22))
            beam.setColorAt(1.0, QColor(r, g, b, 0))
            p.fillRect(QRectF(0, self._beam_y - self.BEAM, self.width(), self.BEAM * 2), beam)
            p.setPen(QPen(QColor(r, g, b, 60), 1))
            p.drawLine(QPointF(0, self._beam_y), QPointF(self.width(), self._beam_y))
        for mx, my, _, _, phase in self._motes:
            twinkle = 0.5 + 0.5 * math.sin(now * 1.7 + phase)
            glow = QRadialGradient(QPointF(mx, my), 5)
            glow.setColorAt(0, QColor(r, g, b, int(40 + 110 * twinkle)))
            glow.setColorAt(1, QColor(r, g, b, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(QPointF(mx, my), 5, 5)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for x, y, started in self._pulses:
            t = (now - started) / 2.4
            alpha = int(90 * math.sin(math.pi * max(0.0, min(1.0, t))))
            hexagon = QPainterPath()
            for i in range(7):
                a = math.radians(60 * i + 30)
                point = QPointF(x + 22 * math.cos(a), y + 22 * math.sin(a))
                hexagon.moveTo(point) if i == 0 else hexagon.lineTo(point)
            p.setPen(QPen(QColor(r, g, b, alpha), 1.4))
            p.fillPath(hexagon, QColor(r, g, b, alpha // 4))
            p.drawPath(hexagon)


class HudPanel(QFrame):
    """A glassy panel: translucent fill, double frame, bright corner brackets, a clipped corner
    and a title tab with a gold marker."""

    def __init__(self, title: str = "", parent: QWidget | None = None, accent: str = ACCENT) -> None:
        super().__init__(parent)
        self.title = title.upper()
        self.accent = QColor(accent)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 34 if title else 16, 16, 14)
        self.body.setSpacing(8)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        cut = 14.0
        path = QPainterPath(QPointF(r.left(), r.top()))
        path.lineTo(r.right() - cut, r.top())
        path.lineTo(r.right(), r.top() + cut)
        path.lineTo(r.right(), r.bottom())
        path.lineTo(r.left() + cut, r.bottom())
        path.lineTo(r.left(), r.bottom() - cut)
        path.closeSubpath()
        fill = QLinearGradient(0, r.top(), 0, r.bottom())
        fill.setColorAt(0, QColor(8, 22, 40, 225))
        fill.setColorAt(1, QColor(4, 11, 22, 235))
        p.fillPath(path, fill)
        p.setPen(QPen(QColor(PANEL_EDGE), 1))
        p.drawPath(path)
        inner = QColor(self.accent)
        inner.setAlpha(28)
        p.setPen(QPen(inner, 1))
        p.drawRect(r.adjusted(4, 4, -4, -4))
        p.setPen(QPen(self.accent, 2))
        arm = 18.0
        for x, y, dx, dy in ((r.left(), r.top(), 1, 1), (r.right(), r.bottom(), -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * arm, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * arm))
        p.drawLine(QPointF(r.right() - cut, r.top()), QPointF(r.right(), r.top() + cut))
        p.drawLine(QPointF(r.left(), r.bottom() - cut), QPointF(r.left() + cut, r.bottom()))
        if self.title:
            p.setFont(mono_font(8, bold=True))
            width = p.fontMetrics().horizontalAdvance(self.title)
            tab = QRectF(r.left() + 12, r.top() + 8, width + 26, 18)
            p.fillRect(tab, QColor(56, 189, 248, 30))
            p.fillRect(QRectF(tab.left(), tab.top(), 3, tab.height()), QColor(GOLD))
            p.setPen(self.accent.lighter(130))
            p.drawText(tab.adjusted(12, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self.title)
            faint = QColor(self.accent)
            faint.setAlpha(55)
            p.setPen(QPen(faint, 1))
            y = tab.center().y()
            p.drawLine(QPointF(tab.right() + 8, y), QPointF(r.right() - 26, y))
            p.setPen(QPen(QColor(GOLD), 1))
            for i in range(3):
                x = r.right() - 22 + i * 5
                p.drawLine(QPointF(x, y - 3), QPointF(x, y + 3))
        p.end()


class SegmentBar(QWidget):
    """LABEL        value   caption
       ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱   (segmented meter; same set_value() as RingGauge)"""

    SEGMENTS = 24

    def __init__(self, label: str, color: str = ACCENT, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = label.upper()
        self.color = QColor(color)
        self.value: float | None = None
        self.text = "--"
        self.caption = ""
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: float | None, text: str | None = None, caption: str = "",
                  color: str | None = None) -> None:
        self.value = None if value is None else max(0.0, min(100.0, value))
        self.text = text if text is not None else ("--" if value is None else f"{value:.0f}%")
        self.caption = caption
        if color:
            self.color = QColor(color)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        p.setFont(mono_font(7.5, bold=True))
        p.setPen(self.color)
        p.drawText(QRectF(0, 0, w * 0.45, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.label)
        p.setFont(mono_font(7.5))
        p.setPen(QColor(DIM))
        caption_w = p.fontMetrics().horizontalAdvance(self.caption) + 6 if self.caption else 0
        if self.caption:
            p.drawText(QRectF(0, 0, w, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.caption)
        p.setFont(mono_font(9.5, bold=True))
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(0, 0, w - caption_w, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   self.text)
        gap = 2.0
        seg_w = (w - gap * (self.SEGMENTS - 1)) / self.SEGMENTS
        lit = 0 if self.value is None else round(self.SEGMENTS * self.value / 100)
        for i in range(self.SEGMENTS):
            rect = QRectF(i * (seg_w + gap), 21, seg_w, 9)
            if i < lit:
                c = QColor(self.color)
                c.setAlphaF(0.55 + 0.45 * (i + 1) / max(1, lit))
            else:
                c = QColor(self.color)
                c.setAlpha(28)
            p.fillRect(rect, c)
        p.end()


class RingGauge(QWidget):
    """A circular gauge: value arc over a dim track, the value in the middle, a label below."""

    def __init__(self, label: str, color: str = ACCENT, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = label.upper()
        self.color = QColor(color)
        self.value: float | None = None  # 0..100, None = not available
        self.text = "--"
        self.caption = ""
        self.setMinimumSize(86, 104)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: float | None, text: str | None = None, caption: str = "",
                  color: str | None = None) -> None:
        self.value = None if value is None else max(0.0, min(100.0, value))
        self.text = text if text is not None else ("--" if value is None else f"{value:.0f}%")
        self.caption = caption
        if color:
            self.color = QColor(color)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height() - 26)
        rect = QRectF((self.width() - side) / 2 + 6, 6, side - 12, side - 12)
        track = QPen(QColor("#0d2436"), 6)
        track.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(track)
        p.drawArc(rect, -45 * 16, 270 * 16)  # 270° track, opening at the bottom
        if self.value is not None:
            pen = QPen(self.color, 6)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.drawArc(rect, 225 * 16, int(-270 * 16 * self.value / 100))
        ticks = QPen(QColor(self.color.red(), self.color.green(), self.color.blue(), 70), 1)
        p.setPen(ticks)
        p.drawEllipse(rect.adjusted(9, 9, -9, -9))
        p.setPen(QColor(TEXT))
        p.setFont(mono_font(11, bold=True))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text)
        p.setFont(mono_font(7.5))
        p.setPen(QColor(DIM))
        label_rect = QRectF(0, rect.bottom() + 2, self.width(), 14)
        p.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, self.label)
        if self.caption:
            p.drawText(QRectF(0, label_rect.bottom(), self.width(), 13), Qt.AlignmentFlag.AlignCenter, self.caption)
        p.end()


class Sparkline(QWidget):
    """Recent history as glowing lines with a faint fill. ``add(value)`` for one series (percent);
    ``add(value, second)`` for two (e.g. network down/up, scaled to the largest value seen)."""

    def __init__(self, points: int = 60, color: str = ACCENT, parent: QWidget | None = None,
                 second_color: str | None = None, scale: str = "percent", height: int = 46) -> None:
        super().__init__(parent)
        self.values: deque[float] = deque(maxlen=points)
        self.second: deque[float] = deque(maxlen=points)
        self.color = QColor(color)
        self.second_color = QColor(second_color) if second_color else None
        self.scale = scale  # percent | auto
        self.setFixedHeight(height)

    def add(self, value: float, second: float | None = None) -> None:
        if self.scale == "percent":
            value = max(0.0, min(100.0, value))
        self.values.append(max(0.0, value))
        if second is not None:
            self.second.append(max(0.0, second))
        self.update()

    def _top(self) -> float:
        if self.scale == "percent":
            return 100.0
        return max([1.0, *self.values, *self.second]) * 1.15

    def _line(self, values, w: float, h: float, top: float) -> list[QPointF]:
        step = w / (self.values.maxlen - 1)
        start = w - step * (len(values) - 1)
        return [QPointF(start + i * step, h - 2 - (h - 4) * v / top) for i, v in enumerate(values)]

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor("#0b2031"), 1))
        for i in range(1, 4):
            p.drawLine(QPointF(0, h * i / 4), QPointF(w, h * i / 4))
        for i in range(1, 8):
            p.drawLine(QPointF(w * i / 8, 0), QPointF(w * i / 8, h))
        top = self._top()
        for values, color in ((self.second, self.second_color), (self.values, self.color)):
            if color is None or len(values) < 2:
                continue
            points = self._line(values, w, h, top)
            line = QPainterPath(points[0])
            for point in points[1:]:
                line.lineTo(point)
            fill = QPainterPath(line)
            fill.lineTo(points[-1].x(), h)
            fill.lineTo(points[0].x(), h)
            fill.closeSubpath()
            gradient = QLinearGradient(0, 0, 0, h)
            upper = QColor(color)
            upper.setAlpha(80)
            gradient.setColorAt(0, upper)
            gradient.setColorAt(1, QColor(0, 0, 0, 0))
            p.fillPath(fill, gradient)
            p.setPen(QPen(color, 1.6))
            p.drawPath(line)
            p.setBrush(color)
            p.drawEllipse(points[-1], 2.4, 2.4)
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.end()


class MiniBar(QWidget):
    """A thin inline bar (process CPU share)."""

    def __init__(self, color: str = ACCENT, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.color = QColor(color)
        self.value = 0.0
        self.setFixedSize(46, 6)

    def set_value(self, value: float) -> None:
        self.value = max(0.0, min(100.0, value))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        track = QColor(self.color)
        track.setAlpha(30)
        p.fillRect(self.rect(), track)
        p.fillRect(QRectF(0, 0, self.width() * self.value / 100, self.height()), self.color)
        p.end()


class StatRow(QWidget):
    """LABEL ............ value"""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.name = QLabel(label.upper())
        self.name.setObjectName("hudLabel")
        self.value = QLabel("--")
        self.value.setObjectName("hudValue")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.name)
        layout.addStretch()
        layout.addWidget(self.value)

    def set(self, text: str) -> None:
        self.value.setText(text)

    def text(self) -> str:
        return self.value.text()
