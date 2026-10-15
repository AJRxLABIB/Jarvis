"""Command-center building blocks (the main window's dashboard look): framed panels with a title
strip and corner brackets, status rows, module rows, thin meters, an event log, coloured tabs,
status cards and the "power core" gauge. Everything shows real data from this PC - no placeholders
pretending to be measurements."""

from __future__ import annotations

import math
import time
import weakref
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QTextBrowser, QVBoxLayout, QWidget,
)

from app.ui.hud import mono_font as _mono


def mono_font(size: float, bold: bool = False, spacing: float = 0.0):
    """The HUD's monospaced font; ``spacing`` adds letter spacing in pixels (the dashboard's spaced titles)."""
    from PySide6.QtGui import QFont

    font = _mono(size, bold)
    if spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return font

CYAN = "#22d3ee"
BLUE = "#38bdf8"
ORANGE = "#f59e0b"
GREEN = "#34d399"
RED = "#f87171"
PURPLE = "#a78bfa"
GOLD = "#f5b642"
DIM = "#6b8aa6"
TEXT = "#cfe3f3"
EDGE = "#0f3a55"

STATUS_COLORS = {"ok": GREEN, "warn": ORANGE, "bad": RED, "off": DIM, "info": CYAN}


class DashPanel(QFrame):
    """A dark framed panel: letter-spaced cyan TITLE with a rule under it, a cyan corner at the top left and
    an orange one at the bottom right. At start it draws itself in (frame, corners, the title typed out),
    one panel after another; now and then a light runs along the title rule."""

    BOOT_SECONDS = 0.75
    SHIMMER_SECONDS = 1.4
    RUN_SECONDS = 1.6  # one pass of the running light while Jarvis thinks / speaks
    _last_created = 0.0
    _order = 0
    _all: "weakref.WeakSet[DashPanel]" = weakref.WeakSet()
    activity = "IDLE"  # THINKING / SPEAKING: a light runs along every title line

    @classmethod
    def replay_boot(cls) -> None:
        """Draw every panel in again, top to bottom (when the window appears after the intro)."""
        from app.ui import motion

        now = time.monotonic()
        panels = [p for p in list(cls._all) if p.isVisible()]
        panels.sort(key=lambda p: (p.mapToGlobal(p.rect().topLeft()).y(), p.mapToGlobal(p.rect().topLeft()).x()))
        for i, panel in enumerate(panels):
            panel._born = now + 0.06 * min(i, 16)
            if motion.ENABLED:
                motion.CLOCK.join(panel._tick)

    @classmethod
    def set_activity(cls, state: str) -> None:
        from app.ui import motion

        cls.activity = state
        if state in ("THINKING", "SPEAKING") and motion.ENABLED:
            for panel in list(cls._all):
                motion.CLOCK.join(panel._tick)

    def __init__(self, title: str = "", accent: str = CYAN, parent: QWidget | None = None,
                 margins: tuple[int, int, int, int] = (10, 8, 10, 10)) -> None:
        super().__init__(parent)
        self.title = title.upper()
        self.accent = QColor(accent)
        self.body = QVBoxLayout(self)
        left, top, right, bottom = margins
        self.body.setContentsMargins(left, top + (26 if title else 0), right, bottom)
        self.body.setSpacing(4)
        from app.ui import motion

        now = time.monotonic()
        if now - DashPanel._last_created > 2.0:
            DashPanel._order = 0  # a new window: start the sequence again
        DashPanel._last_created = now
        self._born = now + 0.08 * min(DashPanel._order, 14)
        DashPanel._order += 1
        self._shimmer_at: float | None = None
        self._shimmer_planned = False
        self._hover = motion.Tween(0.0, seconds=0.25)
        DashPanel._all.add(self)
        if motion.ENABLED:
            motion.CLOCK.join(self._tick)
        else:
            self._born = 0.0

    def enterEvent(self, event) -> None:  # noqa: N802
        from app.ui import motion

        self._hover.set(1.0)
        motion.CLOCK.join(self._tick)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        from app.ui import motion

        self._hover.set(0.0)
        motion.CLOCK.join(self._tick)
        super().leaveEvent(event)

    def boot_progress(self, now: float | None = None) -> float:
        from app.ui import motion

        if not motion.ENABLED:
            return 1.0
        return max(0.0, min(1.0, ((now or time.monotonic()) - self._born) / self.BOOT_SECONDS))

    def _tick(self, now: float) -> bool:
        booting = self.boot_progress(now) < 1.0
        shining = self._shimmer_at is not None and now - self._shimmer_at < self.SHIMMER_SECONDS
        if self._shimmer_at is not None and not shining:
            self._shimmer_at = None
        self._hover.step(now)
        running = DashPanel.activity in ("THINKING", "SPEAKING") and bool(self.title)
        if self.isVisible() and not self.window().isMinimized():
            if booting or self._hover.moving:
                self.update()
            elif shining or running:
                self.update(0, 18, self.width(), 12)  # only the title line the light runs along
            elif getattr(self, "_was_lit", False):
                self.update(0, 18, self.width(), 12)  # once more, to clear the last light
        self._was_lit = shining or running
        busy = booting or shining or self._hover.moving or running
        if not busy and not self._shimmer_planned:
            self._shimmer_planned = True
            self._schedule_shimmer()
        return busy

    def _schedule_shimmer(self) -> None:
        import random

        from PySide6.QtCore import QTimer

        if self.title:
            QTimer.singleShot(int(random.uniform(9, 20) * 1000), self, self._shimmer)  # cancelled if the panel goes

    def _shimmer(self) -> None:
        from app.ui import motion

        self._shimmer_planned = False
        if not motion.ENABLED or not self.isVisible() or self.window().isMinimized():
            self._schedule_shimmer()  # try again later, without drawing anything now
            return
        self._shimmer_at = time.monotonic()
        motion.CLOCK.join(self._tick)

    def paintEvent(self, _event) -> None:  # noqa: N802
        from app.ui.motion import ease_out

        now = time.monotonic()
        grow = ease_out(self.boot_progress(now))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        hover = self._hover.value or 0.0
        p.fillRect(r, QColor(3, 13 + int(6 * hover), 24 + int(10 * hover), int(232 * min(1.0, grow * 1.6))))
        edge = _mix(QColor(EDGE), self.accent, 0.7 * hover)
        edge.setAlphaF(grow)
        p.setPen(QPen(edge, 1 + hover))
        p.drawRect(r)
        if self.title:
            shown = self.title[: max(0, round(len(self.title) * min(1.0, grow * 1.3)))]
            p.setFont(mono_font(8, bold=True, spacing=2.5))
            p.setPen(self.accent)
            cursor = "\u258c" if grow < 1.0 and shown != self.title else ""
            p.drawText(QRectF(r.left() + 9, r.top() + 4, r.width() - 18, 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown + cursor)
            line = QColor(self.accent)
            line.setAlpha(90)
            p.setPen(QPen(line, 1))
            y = r.top() + 24
            p.drawLine(QPointF(r.left() + 1, y), QPointF(r.left() + 1 + (r.width() - 2) * grow, y))
            if self._shimmer_at is not None:
                self._paint_shimmer(p, r, y, (now - self._shimmer_at) / self.SHIMMER_SECONDS)
            if DashPanel.activity in ("THINKING", "SPEAKING") and grow >= 1.0:
                speed = self.RUN_SECONDS * (0.7 if DashPanel.activity == "THINKING" else 1.0)
                self._paint_shimmer(p, r, y, ((now + 0.37 * (id(self) % 7)) % speed) / speed)
        arm = (14.0 + 10.0 * hover) * grow
        p.setPen(QPen(self.accent, 2))
        p.drawLine(QPointF(r.left(), r.top()), QPointF(r.left() + arm, r.top()))
        p.drawLine(QPointF(r.left(), r.top()), QPointF(r.left(), r.top() + arm))
        p.setPen(QPen(QColor(ORANGE), 2))
        p.drawLine(QPointF(r.right(), r.bottom()), QPointF(r.right() - arm, r.bottom()))
        p.drawLine(QPointF(r.right(), r.bottom()), QPointF(r.right(), r.bottom() - arm))
        p.end()

    def _paint_shimmer(self, p: QPainter, r: QRectF, y: float, t: float) -> None:
        from app.ui.motion import ease_out

        t = max(0.0, min(1.0, t))  # a frame can land a hair past the end
        x = r.left() + (r.width() + 120) * ease_out(t) - 60
        glow = QLinearGradient(x - 60, y, x + 60, y)
        clear, bright = QColor(self.accent), QColor(self.accent).lighter(160)
        clear.setAlpha(0)
        bright.setAlpha(int(230 * (1 - t)))
        glow.setColorAt(0, clear)
        glow.setColorAt(0.5, bright)
        glow.setColorAt(1, clear)
        p.setPen(QPen(glow, 2))
        p.drawLine(QPointF(max(r.left() + 1, x - 60), y), QPointF(min(r.right() - 1, x + 60), y))


def _mix(a: QColor, b: QColor, amount: float) -> QColor:
    amount = max(0.0, min(1.0, amount))
    return QColor(int(a.red() + (b.red() - a.red()) * amount), int(a.green() + (b.green() - a.green()) * amount),
                  int(a.blue() + (b.blue() - a.blue()) * amount))


class StatusRow(QWidget):
    """LABEL ...................... VALUE (coloured by status)"""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        self.name = QLabel(label.upper())
        self.name.setFont(mono_font(7.5))
        self.name.setStyleSheet(f"color:{TEXT};")
        self.value = QLabel("--")
        self.value.setFont(mono_font(7.5, bold=True))
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.name)
        row.addStretch()
        row.addWidget(self.value)
        self.set("--", "off")

    def set(self, text: str, status: str = "info") -> None:
        # unchanged -> nothing to do: restyling a label makes Qt re-polish and re-lay out its panel
        if getattr(self, "_last", None) == (text, status):
            return
        self._last = (text, status)
        self.value.setText(str(text).upper())
        self.value.setStyleSheet(f"color:{STATUS_COLORS.get(status, status)};")

    def text(self) -> str:
        return self.value.text()


class ModuleRow(StatusRow):
    """● MODULE NAME ................ STATE (the dot shows whether it is on)"""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        self.dot = None
        super().__init__(label, parent)
        self.dot = QLabel("●")
        self.dot.setFont(mono_font(7))
        self.layout().insertWidget(0, self.dot)
        self.layout().insertSpacing(1, 4)
        self.set("--", "off")

    def set(self, text: str, status: str = "info") -> None:
        if getattr(self, "_last_module", None) == (text, status):
            return
        self._last_module = (text, status)
        self._last = None
        super().set(text, status)
        color = STATUS_COLORS.get(status, status)
        if self.dot is not None:
            self.dot.setStyleSheet(f"color:{color};")
        self.value.setStyleSheet(f"color:{TEXT if status in ('ok', 'info') else color};")


class ThinBar(QWidget):
    """LABEL              caption   42%
       ━━━━━━━━━━━━━━━━───────────────  (thin meter)"""

    def __init__(self, label: str, color: str = CYAN, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = label.upper()
        self.color = QColor(color)
        self.value: float | None = None
        self.text = "--"
        self.caption = ""
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: float | None, caption: str = "", color: str | None = None) -> None:
        from app.ui import motion

        self.value = None if value is None else max(0.0, min(100.0, float(value)))
        self.text = "--" if value is None else f"{value:.0f}%"
        self.caption = caption
        if color:
            self.color = QColor(color)
        if not hasattr(self, "_shown"):
            self._shown = motion.Tween(0.0)  # at start the bar fills up from empty
        self._shown.set(self.value)
        if self._shown.moving:
            motion.CLOCK.join(self._tick)
        self.update()

    def _tick(self, now: float) -> bool:
        self._shown.step(now)
        self.update()
        return self._shown.moving

    def shown_value(self) -> float | None:
        """The bar's drawn length: glides to ``value``."""
        shown = getattr(self, "_shown", None)
        return self.value if shown is None or shown.value is None or self.value is None else shown.value

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        w = self.width()
        p.setFont(mono_font(7.5, bold=True))
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(0, 0, w, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.label)
        p.setPen(self.color)
        p.drawText(QRectF(0, 0, w, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.text)
        if self.caption:
            p.setFont(mono_font(6.5))
            p.setPen(QColor(DIM))
            p.drawText(QRectF(0, 0, w - 42, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       self.caption)
        track = QColor(self.color)
        track.setAlpha(35)
        p.fillRect(QRectF(0, 21, w, 3), track)
        shown = self.shown_value()
        if shown is not None:
            p.fillRect(QRectF(0, 21, w * shown / 100, 3), self.color)
            tip = QColor(self.color).lighter(150)
            p.fillRect(QRectF(max(0.0, w * shown / 100 - 2), 20, 2, 5), tip)  # a bright tip on the bar
        p.end()


class EventLog(QTextBrowser):
    """The LIVE EVENT BUS: newest last, time-stamped, coloured by kind; keeps the last 120 lines."""

    KINDS = {"tool": CYAN, "notice": GOLD, "reminder": ORANGE, "state": DIM, "error": RED, "user": BLUE,
             "ok": GREEN}

    def __init__(self, parent: QWidget | None = None, empty: str = "Waiting for events...") -> None:
        super().__init__(parent)
        self.setFont(mono_font(7.5))
        self.setOpenLinks(False)
        self.setStyleSheet("QTextBrowser { background: transparent; border: none; color: #9fb6c9; }")
        self.lines: deque[tuple[str, str, str]] = deque(maxlen=120)
        self.empty = empty
        self._render()

    def add(self, kind: str, text: str) -> None:
        text = " ".join(str(text).split())
        if not text:
            return
        self.lines.append((time.strftime("%H:%M:%S"), kind, text[:220]))
        self._render()

    def clear_lines(self) -> None:
        self.lines.clear()
        self._render()

    def _render(self) -> None:
        from html import escape

        if not self.lines:
            self.setHtml(f"<span style='color:{DIM}'>{escape(self.empty)}</span>")
            return
        rows = [f"<div style='margin:1px 0'><span style='color:{DIM}'>{t}</span> "
                f"<span style='color:{self.KINDS.get(k, TEXT)}'>{escape(x)}</span></div>" for t, k, x in self.lines]
        self.setHtml("".join(rows))
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


class TabButton(QPushButton):
    """A tab with its own accent line on top; the current tab is framed in orange."""

    def __init__(self, text: str, accent: str, parent: QWidget | None = None) -> None:
        super().__init__(text.upper(), parent)
        self.accent = accent
        self.setFont(mono_font(7.5, bold=True, spacing=1.5))
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_current(False)

    def set_current(self, current: bool) -> None:
        if current:
            self.setStyleSheet(f"QPushButton {{ background: #1a1405; color: #ffffff; border: 1px solid {ORANGE};"
                               f" border-top: 2px solid {ORANGE}; border-radius: 0; padding: 6px 4px; }}")
        else:
            self.setStyleSheet(f"QPushButton {{ background: #04101c; color: #9fb6c9; border: 1px solid {EDGE};"
                               f" border-top: 2px solid {self.accent}; border-radius: 0; padding: 6px 4px; }}"
                               f" QPushButton:hover {{ color: #ffffff; border-color: {self.accent}; }}")


class StatusCard(QFrame):
    """A bottom card: coloured top line, TITLE and status word, one line of detail and an action button."""

    clicked = Signal()

    def __init__(self, title: str, accent: str, action: str = "CHECK", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.accent = accent
        self.setObjectName("statusCard")
        self.setStyleSheet(f"QFrame#statusCard {{ background: rgba(3,13,24,232); border: 1px solid {EDGE};"
                           f" border-top: 2px solid {accent}; }}")
        self.title = QLabel(title.upper())
        self.title.setFont(mono_font(8, bold=True, spacing=2.0))
        self.title.setStyleSheet("color:#e8f6ff; border:none;")
        self.detail = QLabel("--")
        self.detail.setFont(mono_font(7.5))
        self.detail.setStyleSheet(f"color:{DIM}; border:none;")
        self.status = QLabel("--")
        self.status.setFont(mono_font(7, bold=True))
        self.button = QPushButton(action)
        self.button.setFont(mono_font(7, bold=True))
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setStyleSheet(f"QPushButton {{ background: transparent; border: none; color: #e8f6ff;"
                                  f" padding: 0 2px; }} QPushButton:hover {{ color: {accent}; }}")
        self.button.clicked.connect(self.clicked)
        top = QHBoxLayout()
        top.addWidget(self.title)
        top.addStretch()
        top.addWidget(self.status)
        bottom = QHBoxLayout()
        bottom.addWidget(self.detail, 1)
        bottom.addWidget(self.button)
        box = QVBoxLayout(self)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(6)
        box.addLayout(top)
        box.addLayout(bottom)

    def set(self, detail: str, status: str, kind: str = "info") -> None:
        if getattr(self, "_last", None) == (detail, status, kind):
            return
        self._last = (detail, status, kind)
        self.detail.setText(detail)
        self.status.setText(status.upper())
        self.status.setStyleSheet(f"color:{STATUS_COLORS.get(kind, kind)}; border:none;")


class CoreGauge(QWidget):
    """The POWER CORE: a dashed outer ring, a solid ring filled to the value, the value and a word inside."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.value: float | None = None
        self.text, self.word = "--", ""
        self.color = QColor(BLUE)
        self.setMinimumSize(120, 120)
        from app.ui import motion

        self._shown = motion.Tween(0.0, seconds=0.8)

    def set_value(self, value: float | None, text: str, word: str, color: str = BLUE) -> None:
        from app.ui import motion

        self.value = None if value is None else max(0.0, min(100.0, value))
        self.text, self.word, self.color = text, word, QColor(color)
        self._shown.set(self.value)
        if self._shown.moving:
            motion.CLOCK.join(self._tick)
        self.update()

    def _tick(self, now: float) -> bool:
        self._shown.step(now)
        if self.isVisible() and not self.window().isMinimized():
            self.update()
        return self._shown.moving or self._spinning

    @property
    def _spinning(self) -> bool:
        from app.ui import motion

        return motion.ENABLED and self.isVisible()

    def showEvent(self, event) -> None:  # noqa: N802
        from app.ui import motion

        motion.CLOCK.join(self._tick)
        super().showEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 8
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        spin = (time.monotonic() * 30) % 360  # the dashed ring turns slowly; a spark orbits it
        p.save()
        p.translate(rect.center())
        p.rotate(spin)
        dashed = QPen(QColor(CYAN), 2)
        dashed.setDashPattern([3, 4])
        p.setPen(dashed)
        p.drawEllipse(QPointF(0, 0), side / 2, side / 2)
        p.restore()
        orbit = math.radians(-spin * 2.2)
        spark = QPointF(rect.center().x() + side / 2 * math.cos(orbit), rect.center().y() + side / 2 * math.sin(orbit))
        glow = QRadialGradient(spark, 7)
        glow.setColorAt(0, QColor("#e8fbff"))
        glow.setColorAt(1, QColor(34, 211, 238, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(spark, 7, 7)
        p.setBrush(Qt.BrushStyle.NoBrush)
        inner = rect.adjusted(side * 0.08, side * 0.08, -side * 0.08, -side * 0.08)
        p.setPen(QPen(QColor("#0b2a44"), side * 0.07))
        p.drawEllipse(inner)
        shown = getattr(self, "_shown", None)
        value = self.value if shown is None or shown.value is None or self.value is None else shown.value
        if value is not None:
            pen = QPen(self.color, side * 0.07)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.drawArc(inner, 90 * 16, int(-360 * 16 * value / 100))
            end = math.radians(90 - 360 * value / 100)  # a bright tip at the end of the arc
            tip = QPointF(inner.center().x() + inner.width() / 2 * math.cos(end),
                          inner.center().y() - inner.height() / 2 * math.sin(end))
            p.setPen(QPen(QColor("#e8fbff"), side * 0.035))
            p.drawPoint(tip)
        p.setPen(QColor("#e8f6ff"))
        p.setFont(mono_font(9, bold=True))
        p.drawText(QRectF(inner.left(), inner.center().y() - 16, inner.width(), 16), Qt.AlignmentFlag.AlignCenter,
                   self.text)
        p.setFont(mono_font(7.5))
        p.drawText(QRectF(inner.left(), inner.center().y() + 1, inner.width(), 16), Qt.AlignmentFlag.AlignCenter,
                   self.word)
        p.end()


class LogoRing(QWidget):
    """The small reactor mark in the header."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(36, 36)

    def showEvent(self, event) -> None:  # noqa: N802
        from app.ui import motion

        motion.CLOCK.join(self._tick)
        super().showEvent(event)

    def _tick(self, now: float) -> bool:
        from app.ui import motion

        if self.isVisible() and not self.window().isMinimized():
            self.update()
        return motion.ENABLED and self.isVisible()

    def paintEvent(self, _event) -> None:  # noqa: N802
        """Three rings turning against each other (still when animations are off)."""
        from app.ui import motion

        spin = (time.monotonic() * 90) % 360 if motion.ENABLED else 0.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(3, 3, 30, 30)
        for rect, color, width, speed, gap in ((r, CYAN, 2.0, 1.0, 50), (r.adjusted(6, 6, -6, -6), PURPLE, 2.0, -1.6, 90),
                                               (r.adjusted(11, 11, -11, -11), GOLD, 1.5, 2.4, 120)):
            pen = QPen(QColor(color), width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, int((spin * speed) * 16), int((360 - gap) * 16))
        p.end()


class ShineTitle(QWidget):
    """The name in the header, letter-spaced, with a light that sweeps across it every few seconds."""

    SWEEP_SECONDS = 1.1

    def __init__(self, text: str, suffix: str = "//", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.text, self.suffix = text, suffix
        self._sweep_at: float | None = None
        from PySide6.QtGui import QFontMetrics
        from PySide6.QtWidgets import QSizePolicy as _Policy

        metrics = QFontMetrics(self._font())
        self.setFixedHeight(metrics.height() + 4)
        self.setMinimumWidth(metrics.horizontalAdvance(self.text + "  " + self.suffix) + 8)
        self.setSizePolicy(_Policy.Policy.Preferred, _Policy.Policy.Fixed)
        from PySide6.QtCore import QTimer

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sweep)
        self._timer.start(7000)

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QFontMetrics

        metrics = QFontMetrics(self._font())
        return QSize(metrics.horizontalAdvance(self.text + "  " + self.suffix) + 8, metrics.height() + 4)

    def _font(self):
        return mono_font(15, bold=True, spacing=3)

    def _sweep(self) -> None:
        from app.ui import motion

        if motion.ENABLED and self.isVisible() and not self.window().isMinimized():
            self._sweep_at = time.monotonic()
            motion.CLOCK.join(self._tick)

    def _tick(self, now: float) -> bool:
        self.update()
        return self._sweep_at is not None and now - self._sweep_at < self.SWEEP_SECONDS

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(self._font())
        metrics = p.fontMetrics()
        base_y = (self.height() + metrics.ascent() - metrics.descent()) / 2
        p.setPen(QColor("#e6f6ff"))
        p.drawText(QPointF(0, base_y), self.text)
        p.setPen(QColor(ORANGE))
        p.drawText(QPointF(metrics.horizontalAdvance(self.text + "  "), base_y), self.suffix)
        if self._sweep_at is not None:
            t = (time.monotonic() - self._sweep_at) / self.SWEEP_SECONDS
            if 0 <= t <= 1:
                width = metrics.horizontalAdvance(self.text)
                x = -60 + (width + 120) * t
                light = QLinearGradient(x - 50, 0, x + 50, 0)
                light.setColorAt(0, QColor(255, 255, 255, 0))
                light.setColorAt(0.5, QColor(255, 255, 255, 235))
                light.setColorAt(1, QColor(255, 255, 255, 0))
                path = QPainterPath()
                path.addText(QPointF(0, base_y), self._font(), self.text)
                p.fillPath(path, light)
        p.end()


class LevelBars(QWidget):
    """||||| a small live microphone level (in the card over the core)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.levels: deque[float] = deque([0.0] * 8, maxlen=8)
        self.setFixedSize(64, 26)

    def push(self, level: float) -> None:
        self.levels.append(max(0.0, min(1.0, level)))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        for i, level in enumerate(self.levels):
            h = 4 + 20 * level
            p.fillRect(QRectF(i * 8, 26 - h, 3, h), QColor(CYAN))
        p.end()
