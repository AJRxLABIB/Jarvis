"""The start-up intro: a full-screen, ~4 second sequence drawn in code.

Hex grid lights up from the centre -> HUD rings assemble and spin (ticks, sweeping arcs, a gold dashed
ring, a radar sweep, orbiting points) -> the name decodes letter by letter -> a boot log shows the REAL
state of each part as it is at that moment (speech output, speech recognition, the AI model, memory),
next to live CPU / RAM from this PC -> a flash, the rings fly outward and the command center fades in.

Nothing here is a fake measurement: a part that is still starting says STARTING, and updates live.
Any key or click skips to the end. INTRO_ANIMATION=false turns it off.
"""

from __future__ import annotations

import math
import random
import time
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QConicalGradient, QFont, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget

CYAN = QColor("#22d3ee")
BLUE = QColor("#38bdf8")
GOLD = QColor("#f5b642")
ORANGE = QColor("#f59e0b")
GREEN = QColor("#34d399")
RED = QColor("#f87171")
DIM = QColor("#5f7f9c")
TEXT = QColor("#d6e6f5")
KIND_COLORS = {"ok": GREEN, "warn": ORANGE, "bad": RED, "wait": GOLD, "info": CYAN}
GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#%&@$"

DURATION = 4.4
FINALE = 3.55  # the flash; the command center starts fading in here
FADE_OUT = 0.75
HOLD = 3.2  # waiting for Jarvis to finish loading: the sequence pauses here, the rings keep turning
MAX_WAIT = 25.0  # ...but never longer than this

StatusLine = tuple[str, Callable[[], tuple[str, str]]]  # label, () -> (text, kind)


def mono(size: float, bold: bool = False, spacing: float = 0.0) -> QFont:
    font = QFont()
    font.setFamilies(["Cascadia Mono", "Consolas", "Courier New"])
    font.setPointSizeF(size)
    font.setBold(bold)
    if spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return font


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def ease(x: float) -> float:
    x = clamp01(x)
    return 1 - (1 - x) ** 3


def span(t: float, start: float, length: float) -> float:
    """0..1 progress of a step that starts at ``start`` and lasts ``length`` seconds."""
    return clamp01((t - start) / length)


class IntroScreen(QWidget):
    finished = Signal()  # the command center should appear now (the intro keeps fading out on top)

    def __init__(self, title: str, subtitle: str, status_lines: list[StatusLine],
                 clock: Callable[[], float] = time.monotonic, wait_for_ready: bool = False) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.title = title.upper()
        self.subtitle = subtitle.upper()
        self.lines = status_lines
        self._clock = clock
        self._t0: float | None = None
        self._spin0: float | None = None
        self._waiting = wait_for_ready  # hold before the finale until ready() (Jarvis has loaded)
        self._skip_to: float | None = None
        self._finished_sent = False
        self._grid: QPixmap | None = None
        self._rng = random.Random(7)
        self._system = ("--", "--")
        self._system_at = -1.0
        self._timer = None
        try:  # psutil's first CPU reading is always 0: take it now, so the first one shown is real
            import psutil

            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    # ------------------------------------------------------------ running
    def start(self) -> None:
        from PySide6.QtCore import QTimer

        self._t0 = self._spin0 = self._clock()
        self.showFullScreen()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)  # ~60 fps for four seconds
        self._timer.timeout.connect(self._frame)
        self._timer.start()

    def elapsed(self) -> float:
        """Where the sequence is (it pauses at HOLD while waiting for ready())."""
        if self._t0 is None:
            return 0.0
        t = self._clock() - self._t0
        if self._waiting and self._spin_elapsed() > MAX_WAIT:
            self.ready()  # don't wait for ever
            t = self._clock() - self._t0
        return min(t, HOLD) if self._waiting else t

    def _spin_elapsed(self) -> float:
        return 0.0 if self._spin0 is None else self._clock() - self._spin0

    def ready(self) -> None:
        """Jarvis has loaded: go on to the finale (from the hold point if the sequence is waiting there)."""
        if not self._waiting:
            return
        self._waiting = False
        if self._t0 is not None and self._clock() - self._t0 > HOLD:
            self._t0 = self._clock() - HOLD

    def skip(self) -> None:
        if self._t0 is None:
            return
        target = HOLD if self._waiting else FINALE
        if self.elapsed() < target:
            self._t0 = self._clock() - target - 1e-6  # (a hair past it: floating point)

    def _frame(self) -> None:
        t = self.elapsed()
        if t >= FINALE and not self._finished_sent:
            self._finished_sent = True
            self.finished.emit()
        if t >= DURATION:
            if self._timer is not None:
                self._timer.stop()
            self.close()
            return
        self.setWindowOpacity(1.0 - ease(span(t, DURATION - FADE_OUT, FADE_OUT)))
        self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        self.skip()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.skip()

    # ------------------------------------------------------------ drawing
    def paintEvent(self, _event) -> None:  # noqa: N802
        self.paint(QPainter(self), self.elapsed(), self._spin_elapsed())

    def paint(self, p: QPainter, t: float, spin: float | None = None) -> None:
        """Everything at sequence time ``t``; ``spin`` turns the rings (it keeps going during a hold)."""
        spin = t if spin is None else spin
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        c = QPointF(w / 2, h * 0.40)
        radius = min(w, h) * 0.25
        p.fillRect(self.rect(), QColor("#01040a"))
        self._paint_grid(p, t, c, w, h)
        grow = 1.0 + 0.9 * ease(span(t, FINALE, 0.7))  # the rings fly outward at the end
        fade = 1.0 - ease(span(t, FINALE + 0.05, 0.6))
        p.save()
        p.setOpacity(fade)
        self._paint_rings(p, t, c, radius * grow, spin)
        p.restore()
        self._paint_title(p, t, c, radius)
        self._paint_log(p, t, w, h)
        self._paint_system(p, t, w, h)
        self._paint_timeline(p, t, w, h)
        self._paint_flash(p, t, c, max(w, h))
        p.end()

    def _paint_grid(self, p: QPainter, t: float, c: QPointF, w: int, h: int) -> None:
        if self._grid is None or self._grid.size() != self.size():
            self._grid = self._render_grid(w, h)
        reveal = ease(span(t, 0.0, 1.1))
        if reveal <= 0:
            return
        if reveal >= 1:
            p.drawPixmap(0, 0, self._grid)  # the finished background: one copy per frame, nothing recomputed
            return
        p.save()
        clip = QPainterPath()
        r = max(w, h) * reveal
        clip.addEllipse(c, r, r)
        p.setClipPath(clip)
        p.setOpacity(0.35 + 0.65 * reveal)
        p.drawPixmap(0, 0, self._grid)
        p.restore()
        ring = QPen(QColor(34, 211, 238, int(120 * (1 - reveal))), 2)  # the edge of the reveal
        p.setPen(ring)
        p.drawEllipse(c, r, r)

    def _render_grid(self, w: int, h: int) -> QPixmap:
        pixmap = QPixmap(max(1, w), max(1, h))
        pixmap.fill(Qt.GlobalColor.transparent)
        g = QPainter(pixmap)
        g.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = 26.0
        dx, dy = size * math.sqrt(3), size * 1.5
        g.setPen(QPen(QColor(56, 189, 248, 22), 1))
        y, row = 0.0, 0
        while y < h + size:
            x = (dx / 2 if row % 2 else 0.0) - dx
            while x < w + dx:
                hexagon = QPainterPath()
                for i in range(7):
                    a = math.radians(60 * i + 30)
                    point = QPointF(x + size * math.cos(a), y + size * math.sin(a))
                    hexagon.moveTo(point) if i == 0 else hexagon.lineTo(point)
                g.drawPath(hexagon)
                x += dx
            y += dy
            row += 1
        glow = QRadialGradient(QPointF(w / 2, h * 0.40), max(w, h) * 0.6)
        glow.setColorAt(0, QColor(34, 211, 238, 40))
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        g.fillRect(0, 0, w, h, glow)
        vignette = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * 0.7)
        vignette.setColorAt(0.55, QColor(0, 0, 0, 0))
        vignette.setColorAt(1.0, QColor(0, 0, 0, 200))
        g.fillRect(0, 0, w, h, vignette)
        g.end()
        return pixmap

    def _paint_rings(self, p: QPainter, t: float, c: QPointF, R: float, s: float) -> None:
        def arc(radius: float, start_deg: float, extent_deg: float, pen: QPen) -> None:
            p.setPen(pen)
            p.drawArc(QRectF(c.x() - radius, c.y() - radius, 2 * radius, 2 * radius),
                      int(start_deg * 16), int(extent_deg * 16))

        # 1. tick ring, drawn round in 0.8 s, then turning slowly
        ticks = span(t, 0.3, 0.8)
        if ticks > 0:
            turn = 12 * s
            for i in range(int(144 * ticks)):
                a = math.radians(i * 2.5 + turn)
                long = i % 12 == 0
                inner = R * (0.955 if long else 0.975)
                color = QColor(CYAN if long else BLUE)
                color.setAlpha(210 if long else 110)
                p.setPen(QPen(color, 2 if long else 1))
                p.drawLine(QPointF(c.x() + inner * math.cos(a), c.y() + inner * math.sin(a)),
                           QPointF(c.x() + R * math.cos(a), c.y() + R * math.sin(a)))
        # 2. three bright arcs sweeping open, turning the other way
        grow = ease(span(t, 0.5, 0.6))
        if grow > 0:
            pen = QPen(CYAN, 3.2)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            for k in range(3):
                arc(R * 0.9, -40 * s + k * 120, 72 * grow, pen)
            faint = QColor(CYAN)
            faint.setAlpha(45)
            arc(R * 0.9, 0, 360, QPen(faint, 1))
        # 3. gold dashed ring
        dashed = span(t, 0.7, 0.4)
        if dashed > 0:
            gold = QColor(GOLD)
            gold.setAlphaF(0.85 * dashed)
            pen = QPen(gold, 2.2)
            pen.setDashPattern([2, 5])
            p.save()
            p.translate(c)
            p.rotate(25 * s)
            p.setPen(pen)
            p.drawEllipse(QPointF(0, 0), R * 0.79, R * 0.79)
            p.restore()
        # 4. thick segmented band
        band = ease(span(t, 0.9, 0.5))
        if band > 0:
            color = QColor(BLUE)
            color.setAlpha(int(150 * band))
            pen = QPen(color, R * 0.045)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            for k in range(8):
                arc(R * 0.67, 15 * s + k * 45, 30 * band, pen)
        # 5. radar sweep with a crosshair
        radar = span(t, 1.0, 0.4)
        if radar > 0:
            r5 = R * 0.52
            sweep = QConicalGradient(c, -200 * s)
            head = QColor(CYAN)
            head.setAlpha(int(130 * radar))
            sweep.setColorAt(0.0, head)
            sweep.setColorAt(0.18, QColor(34, 211, 238, 0))
            sweep.setColorAt(1.0, QColor(34, 211, 238, 0))
            path = QPainterPath()
            path.addEllipse(c, r5, r5)
            p.fillPath(path, sweep)
            line = QColor(CYAN)
            line.setAlpha(int(60 * radar))
            p.setPen(QPen(line, 1))
            p.drawEllipse(c, r5, r5)
            p.drawLine(QPointF(c.x() - r5, c.y()), QPointF(c.x() + r5, c.y()))
            p.drawLine(QPointF(c.x(), c.y() - r5), QPointF(c.x(), c.y() + r5))
        # 6. orbiting points
        orbit = span(t, 1.1, 0.3)
        if orbit > 0:
            for k, (speed, radius, color) in enumerate(((70, 0.9, CYAN), (-55, 0.79, GOLD), (95, 0.67, BLUE))):
                a = math.radians(speed * s + k * 130)
                point = QPointF(c.x() + R * radius * math.cos(a), c.y() + R * radius * math.sin(a))
                glow = QRadialGradient(point, 14)
                bright = QColor(color)
                bright.setAlphaF(orbit)
                glow.setColorAt(0, bright)
                glow.setColorAt(1, QColor(0, 0, 0, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(glow)
                p.drawEllipse(point, 14, 14)
            p.setBrush(Qt.BrushStyle.NoBrush)
        # 7. the core: a pulsing light in the middle
        core = ease(span(t, 1.2, 0.5))
        if core > 0:
            pulse = 0.85 + 0.15 * math.sin(s * 6)
            glow = QRadialGradient(c, R * 0.34 * pulse)
            glow.setColorAt(0.0, QColor(230, 250, 255, int(230 * core)))
            glow.setColorAt(0.25, QColor(34, 211, 238, int(170 * core)))
            glow.setColorAt(1.0, QColor(34, 211, 238, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(c, R * 0.34 * pulse, R * 0.34 * pulse)
            p.setBrush(Qt.BrushStyle.NoBrush)
        # 8. lens brackets left and right
        brackets = ease(span(t, 1.15, 0.6))
        if brackets > 0:
            color = QColor(CYAN)
            color.setAlpha(170)
            pen = QPen(color, 2)
            arc(R * 1.12, 150, 60 * brackets, pen)
            arc(R * 1.12, -30, 60 * brackets, pen)
            p.setPen(QPen(ORANGE, 2))
            arc(R * 1.18, 175, 10 * brackets, QPen(ORANGE, 2))
            arc(R * 1.18, -5, 10 * brackets, QPen(ORANGE, 2))

    def _paint_title(self, p: QPainter, t: float, c: QPointF, R: float) -> None:
        start = 1.4
        if t < start:
            return
        letters = [ch for ch in self.title]
        shown = []
        for i, ch in enumerate(letters):
            lock = start + 0.07 * i + 0.25
            if ch in " .":
                shown.append(ch)
            elif t >= lock:
                shown.append(ch)
            elif t >= start + 0.05 * i:
                shown.append(self._rng.choice(GLYPHS))
            else:
                shown.append(" ")
        text = "".join(shown)
        font = mono(max(26.0, R * 0.16), bold=True, spacing=R * 0.05)
        p.setFont(font)
        box = QRectF(0, c.y() + R * 1.22, self.width(), R * 0.32)
        settled = t >= start + 0.07 * len(letters) + 0.25
        for spread, alpha in ((6, 30), (3, 60)):  # a soft glow behind the letters
            glow = QColor(CYAN)
            glow.setAlpha(alpha)
            p.setPen(glow)
            for dx, dy in ((spread, 0), (-spread, 0), (0, spread), (0, -spread)):
                p.drawText(box.translated(dx, dy), Qt.AlignmentFlag.AlignCenter, text)
        p.setPen(QColor("#e8fbff") if settled else CYAN)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        if settled:  # a light passes over the name once
            sweep = span(t, start + 0.07 * len(letters) + 0.3, 0.6)
            if 0 < sweep < 1:
                p.save()
                x = box.left() + box.width() * sweep
                clip = QPainterPath()
                clip.addRect(QRectF(x - 40, box.top(), 80, box.height()))
                p.setClipPath(clip)
                p.setPen(QColor(255, 255, 255))
                p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
                p.restore()
        sub = span(t, 2.3, 0.6)
        if sub > 0:
            p.setFont(mono(11, spacing=4))
            p.setPen(DIM.lighter(130))
            typed = self.subtitle[: int(len(self.subtitle) * sub)]
            p.drawText(QRectF(0, box.bottom() + 4, self.width(), 26), Qt.AlignmentFlag.AlignCenter,
                       typed + ("▌" if sub < 1 else ""))

    def _paint_log(self, p: QPainter, t: float, w: int, h: int) -> None:
        x, y = w * 0.05, h * 0.2
        p.setFont(mono(9, bold=True, spacing=2))
        head = span(t, 0.8, 0.3)
        if head <= 0:
            return
        p.setPen(QColor(34, 211, 238, int(255 * head)))
        p.drawText(QPointF(x, y), "BOOT SEQUENCE")
        p.setPen(QPen(QColor(34, 211, 238, int(90 * head)), 1))
        p.drawLine(QPointF(x, y + 8), QPointF(x + 300 * ease(head), y + 8))
        p.setFont(mono(9))
        for i, (label, state) in enumerate(self.lines):
            appear = 1.0 + 0.3 * i
            if t < appear:
                break
            try:
                text, kind = state()
            except Exception:
                text, kind = "UNKNOWN", "warn"
            typed = span(t, appear, 0.25)
            row_y = y + 34 + i * 24
            name = f"> {label.upper()}"
            p.setPen(TEXT)
            p.drawText(QPointF(x, row_y), name[: max(1, int(len(name) * typed))])
            if typed >= 1:
                metrics = p.fontMetrics()
                value_x = x + max(280, metrics.horizontalAdvance(name) + 70)
                dots_from = x + metrics.horizontalAdvance(name) + 10
                dots = max(3, int((value_x - 10 - dots_from) / max(1, metrics.horizontalAdvance("."))))
                p.setPen(QColor(95, 127, 156, 140))
                p.drawText(QPointF(dots_from, row_y), "." * dots)
                p.setPen(KIND_COLORS.get(kind, CYAN))
                p.drawText(QPointF(value_x, row_y), text.upper())

    def _paint_system(self, p: QPainter, t: float, w: int, h: int) -> None:
        show = span(t, 1.2, 0.4)
        if show <= 0:
            return
        now = self._clock()
        if now - self._system_at > 0.5:  # this PC's real load, twice a second
            self._system_at = now
            try:
                import psutil

                self._system = (f"{psutil.cpu_percent(interval=None):.0f}%", f"{psutil.virtual_memory().percent:.0f}%")
            except Exception:
                self._system = ("--", "--")
        x, y = w * 0.95, h * 0.2
        p.setFont(mono(9, bold=True, spacing=2))
        p.setPen(QColor(34, 211, 238, int(255 * show)))
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        p.drawText(QRectF(x - 300, y - 14, 300, 20), right, "THIS PC")
        p.setPen(QPen(QColor(34, 211, 238, int(90 * show)), 1))
        p.drawLine(QPointF(x - 300 * ease(show), y + 8), QPointF(x, y + 8))
        p.setFont(mono(9))
        rows = (("TIME", time.strftime("%H:%M:%S")), ("DATE", time.strftime("%d %b %Y").upper()),
                ("CPU", self._system[0]), ("MEMORY", self._system[1]))
        for i, (label, value) in enumerate(rows):
            if t < 1.3 + 0.2 * i:
                break
            row = QRectF(x - 300, y + 22 + i * 24, 300, 20)
            p.setPen(DIM.lighter(120))
            p.drawText(row, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            p.setPen(TEXT)
            p.drawText(row, right, value)

    def _paint_timeline(self, p: QPainter, t: float, w: int, h: int) -> None:
        show = span(t, 0.2, 0.4)
        if show <= 0:
            return
        bar_w, y = w * 0.36, h * 0.93
        x = (w - bar_w) / 2
        progress = ease(span(t, 0.2, FINALE - 0.3))
        p.fillRect(QRectF(x, y, bar_w, 3), QColor(34, 211, 238, int(40 * show)))
        p.fillRect(QRectF(x, y, bar_w * progress, 3), CYAN)
        p.fillRect(QRectF(x + bar_w * progress - 3, y - 2, 3, 7), QColor("#e8fbff"))
        label = "INTERFACE READY" if progress >= 1 else "INITIALIZING INTERFACE"
        p.setFont(mono(8, bold=True, spacing=3))
        p.setPen(QColor(95, 127, 156, int(255 * show)))
        p.drawText(QRectF(x, y + 10, bar_w, 18), Qt.AlignmentFlag.AlignLeft, label)
        p.drawText(QRectF(x, y + 10, bar_w, 18), Qt.AlignmentFlag.AlignRight, f"{int(progress * 100):3d}%")

    def _paint_flash(self, p: QPainter, t: float, c: QPointF, size: float) -> None:
        f = span(t, FINALE - 0.05, 0.55)
        if f <= 0 or f >= 1:
            return
        strength = math.sin(math.pi * f) ** 2
        glow = QRadialGradient(c, size * (0.2 + 0.9 * f))
        glow.setColorAt(0, QColor(235, 252, 255, int(200 * strength)))
        glow.setColorAt(0.4, QColor(34, 211, 238, int(110 * strength)))
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), glow)
        ring = QColor(34, 211, 238, int(220 * (1 - f)))
        p.setPen(QPen(ring, 3))
        p.drawEllipse(c, size * 0.6 * f, size * 0.6 * f)
