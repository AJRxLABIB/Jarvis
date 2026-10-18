"""The window / tab overview: "show me my screen", "show my windows", "show my tabs".

A full-screen, Mission-Control-style layer: every open window flies smoothly from where it really is into a grid
(live pictures drawn by the Windows compositor, so they keep moving), the background dims, and titles fade in.
Click a window (or use the arrow keys + Enter) and it zooms back to its place and comes to the front; Esc or a
click on the background closes the overview. In tab mode the cards are your browser tabs; picking one switches
the browser to that tab.

Nothing is captured: the live pictures are composed by Windows itself and never pass through Jarvis.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from app.core.overview import (
    OverviewItem, Rect, ease_in_out_cubic, ease_out_cubic, fallback_source, layout, lerp_rect, neighbour,
)
from app.ui.styles.tokens import COLOR

_log = logging.getLogger(__name__)
OPEN_SECONDS = 0.38
CLOSE_SECONDS = 0.26
TOP, BOTTOM, SIDE = 96.0, 64.0, 60.0


class WindowOverview(QWidget):
    chosen = Signal(object)          # the OverviewItem picked (emitted after the zoom-back animation)
    closed = Signal()

    def __init__(self, items: list[OverviewItem], mode: str = "windows",
                 thumbnail_factory: Callable[[int, int], object] | None = None, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Window overview" if mode == "windows" else "Tab overview")
        self.items = items
        self.mode = mode
        self._factory = thumbnail_factory
        self._thumbs: dict[int, object] = {}
        self.targets: list[Rect] = []
        self.sources: list[Rect] = []
        self.hover = -1
        self.selected = 0 if items else -1
        self._phase = "opening"
        self._t0 = time.monotonic()
        self._pick: int | None = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._step)
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self.setGeometry(screen.geometry())
        self._dpr = screen.devicePixelRatio() or 1.0
        self._origin = screen.geometry().topLeft()

    # ------------------------------------------------------------------ geometry
    def _local(self, rect: Rect) -> Rect:
        """Screen (physical pixels, as Windows reports them) -> this widget's coordinates."""
        x, y, w, h = rect
        return (x / self._dpr - self._origin.x(), y / self._dpr - self._origin.y(), w / self._dpr, h / self._dpr)

    def _compute(self) -> None:
        aspects = [max(0.5, min(2.6, item.aspect)) for item in self.items]
        area_w, area_h = self.width() - 2 * SIDE, self.height() - TOP - BOTTOM
        self.targets = [(x + SIDE, y + TOP, w, h) for x, y, w, h in layout(aspects, area_w, area_h)]
        self.sources = []
        for item, target in zip(self.items, self.targets):
            if item.source and not item.minimized and item.source[2] > 0 and item.source[3] > 0:
                self.sources.append(self._local(item.source))
            else:
                self.sources.append(fallback_source(target, self.width(), self.height()))

    # ------------------------------------------------------------------ life cycle
    def open(self) -> None:
        self._compute()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        if self._factory is not None:
            for i, item in enumerate(self.items):
                if item.live and item.hwnd:
                    try:
                        thumb = self._factory(int(self.winId()), item.hwnd)
                        if getattr(thumb, "ok", False):
                            self._thumbs[i] = thumb
                    except Exception:
                        _log.info("no live picture for %s", item.title, exc_info=True)
        self._t0 = time.monotonic()
        self._timer.start()
        self._step()

    def dismiss(self) -> None:
        if self._phase in ("closing", "picking"):
            return
        self._phase = "closing"
        self._t0 = time.monotonic()
        self._timer.start()

    def pick(self, index: int) -> None:
        if not 0 <= index < len(self.items) or self._phase in ("closing", "picking"):
            return
        self._pick = index
        self._phase = "picking"
        self._t0 = time.monotonic()
        self._timer.start()

    def _finish(self) -> None:
        self._timer.stop()
        for thumb in self._thumbs.values():
            try:
                thumb.release()
            except Exception:
                pass
        self._thumbs.clear()
        picked = self.items[self._pick] if self._pick is not None else None
        self.hide()
        if picked is not None:
            self.chosen.emit(picked)
        self.closed.emit()
        self.close()

    # ------------------------------------------------------------------ animation
    def progress(self) -> float:
        """0 = cards at their real places, 1 = cards in the grid."""
        elapsed = time.monotonic() - self._t0
        if self._phase == "opening":
            return ease_out_cubic(elapsed / OPEN_SECONDS)
        if self._phase in ("closing", "picking"):
            return 1.0 - ease_in_out_cubic(elapsed / CLOSE_SECONDS)
        return 1.0

    def card_rect(self, i: int) -> QRectF:
        t = self.progress()
        if self._phase == "picking" and i != self._pick:
            t = 1.0                                       # the others stay and fade out
        x, y, w, h = lerp_rect(self.sources[i], self.targets[i], t)
        if i == self.hover and self._phase == "open":
            grow = 0.035
            x, y, w, h = x - w * grow / 2, y - h * grow / 2, w * (1 + grow), h * (1 + grow)
        return QRectF(x, y, w, h)

    def card_opacity(self, i: int) -> float:
        if self._phase == "picking" and i != self._pick:
            return max(0.0, 1.0 - (time.monotonic() - self._t0) / CLOSE_SECONDS)
        return 1.0

    def _step(self) -> None:
        elapsed = time.monotonic() - self._t0
        if self._phase == "opening" and elapsed >= OPEN_SECONDS:
            self._phase = "open"
            self._timer.stop()
        elif self._phase in ("closing", "picking") and elapsed >= CLOSE_SECONDS:
            self._finish()
            return
        self._place_thumbs()
        self.update()

    def _place_thumbs(self) -> None:
        for i, thumb in self._thumbs.items():
            r = self.card_rect(i).adjusted(1, 1, -1, -1)
            d = self._dpr
            thumb.place(r.x() * d, r.y() * d, r.width() * d, r.height() * d, self.card_opacity(i))

    # ------------------------------------------------------------------ input
    def index_at(self, pos: QPointF) -> int:
        for i in range(len(self.items)):
            if self.card_rect(i).adjusted(-6, -6, 6, 34).contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hover = self.index_at(event.position())
        if hover != self.hover:
            self.hover = hover
            if hover >= 0:
                self.selected = hover
            self._place_thumbs()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        index = self.index_at(event.position())
        if index >= 0:
            self.pick(index)
        else:
            self.dismiss()

    def focusNextPrevChild(self, _next: bool) -> bool:  # noqa: N802
        return False                       # Tab moves between cards (keyPressEvent), not out of the overview

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        moves = {Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right", Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down"}
        if key == Qt.Key.Key_Escape:
            self.dismiss()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.pick(self.selected)
        elif key in moves:
            self.selected = neighbour(self.targets, self.selected, moves[key])
            self.hover = self.selected
            self._place_thumbs()
            self.update()
        elif key == Qt.Key.Key_Tab:
            if self.items:
                self.selected = (self.selected + 1) % len(self.items)
                self.hover = self.selected
                self._place_thumbs()
                self.update()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------ drawing
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.progress()
        p.fillRect(self.rect(), QColor(2, 7, 16, int(205 * t)))           # the dimmed, darkened desktop
        title_font = QFont("Segoe UI", 15)
        title_font.setBold(True)
        p.setFont(title_font)
        heading = (f"WINDOWS  ·  {len(self.items)} OPEN" if self.mode == "windows"
                   else f"BROWSER TABS  ·  {len(self.items)} OPEN")
        p.setPen(QColor(COLOR["cyan"]).lighter(110) if t > 0.2 else Qt.GlobalColor.transparent)
        p.setOpacity(t)
        p.drawText(QRectF(0, 28, self.width(), 40), Qt.AlignmentFlag.AlignCenter, heading)
        p.setFont(QFont("Segoe UI", 9))
        p.setPen(QColor(COLOR["dim"]))
        p.drawText(QRectF(0, self.height() - 46, self.width(), 30), Qt.AlignmentFlag.AlignCenter,
                   "Click to open  ·  arrows + Enter  ·  Esc to close")
        p.setOpacity(1.0)
        for i, item in enumerate(self.items):
            self._paint_card(p, i, item, t)
        p.end()

    def _paint_card(self, p: QPainter, i: int, item: OverviewItem, t: float) -> None:
        rect = self.card_rect(i)
        opacity = self.card_opacity(i)
        p.setOpacity(opacity)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        highlighted = i == self.hover or (i == self.selected and self.hover < 0)
        if highlighted and self._phase == "open":
            glow = QColor(COLOR["cyan"])
            glow.setAlpha(70)
            p.setPen(QPen(glow, 8))
            p.drawPath(path)
        if i not in self._thumbs:                    # no live picture: a title card
            p.fillPath(path, QColor(COLOR["bubble_ai"]))
            p.setPen(QColor(COLOR["cyan"]))
            letter_font = QFont("Segoe UI", max(14, int(rect.height() * 0.22)))
            letter_font.setBold(True)
            p.setFont(letter_font)
            initial = (item.subtitle or item.title or "?")[:1].upper()
            p.drawText(rect.adjusted(0, -rect.height() * 0.12, 0, -rect.height() * 0.12), Qt.AlignmentFlag.AlignCenter,
                       initial)
            p.setFont(QFont("Segoe UI", 10))
            p.setPen(QColor(COLOR["text"]))
            metrics = QFontMetrics(p.font())
            text = metrics.elidedText(item.title, Qt.TextElideMode.ElideRight, int(rect.width() - 24))
            p.drawText(QRectF(rect.x() + 12, rect.center().y() + rect.height() * 0.12, rect.width() - 24, 22),
                       Qt.AlignmentFlag.AlignCenter, text)
        else:
            p.fillPath(path, QColor(0, 0, 0, 120))       # behind the live picture while it scales
        edge = QColor(COLOR["cyan"] if highlighted else COLOR["bubble_ai_edge"])
        p.setPen(QPen(edge, 2 if highlighted else 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        if t > 0.55 and self._phase != "picking":       # the label fades in as the card arrives
            p.setOpacity(opacity * min(1.0, (t - 0.55) / 0.45))
            label_font = QFont("Segoe UI", 10)
            label_font.setBold(highlighted)
            p.setFont(label_font)
            metrics = QFontMetrics(label_font)
            label = item.title if item.kind == "tab" else (item.subtitle or item.title)
            text = metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(max(60, rect.width())))
            p.setPen(QColor(COLOR["text_strong"] if highlighted else COLOR["text"]))
            p.drawText(QRectF(rect.x() - 20, rect.bottom() + 6, rect.width() + 40, 22), Qt.AlignmentFlag.AlignCenter, text)
        p.setOpacity(1.0)


# ---------------------------------------------------------------------- what to show (Windows)
SKIP = {"jarvis.exe", "jarvis-cli.exe", "textinputhost.exe", "shellexperiencehost.exe", "searchhost.exe",
        "startmenuexperiencehost.exe", "lockapp.exe", "applicationframehost.exe"}
APP_NAMES = {"chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Firefox",
             "code.exe": "Visual Studio Code", "explorer.exe": "File Explorer", "spotify.exe": "Spotify",
             "whatsapp.exe": "WhatsApp", "discord.exe": "Discord", "notepad.exe": "Notepad", "brave.exe": "Brave"}


def _app_name(process: str) -> str:
    return APP_NAMES.get(process, process.removesuffix(".exe").replace("_", " ").title() or "App")


def window_items() -> list[OverviewItem]:
    """Every window you could Alt+Tab to (except Jarvis itself), with where it is now."""
    from app.tools import winapi

    items = []
    for window in winapi.visible_windows():
        if window.pid == os.getpid() or window.process_name in SKIP:
            continue
        try:
            r = winapi.window_rect(window.hwnd)
            source = (r.left, r.top, r.right - r.left, r.bottom - r.top)
        except Exception:
            source = None
        aspect = (source[2] / source[3]) if source and source[3] > 0 and not window.minimized else 16 / 10
        items.append(OverviewItem(key=str(window.hwnd), title=window.title, subtitle=_app_name(window.process_name),
                                  hwnd=window.hwnd, source=source, aspect=aspect, minimized=window.minimized))
    return items


def tab_items() -> list[OverviewItem]:
    """Every tab of every open browser window (Chrome, Edge, Brave...). The tab on show gets the live picture."""
    from app.tools import browser_tabs, winapi

    items: list[OverviewItem] = []
    for exe in sorted(browser_tabs.TAB_BROWSERS):
        for window in browser_tabs.browser_windows(exe):
            tabs = browser_tabs.tab_items(window.hwnd) or []
            try:
                r = winapi.window_rect(window.hwnd)
                source = (r.left, r.top, r.right - r.left, r.bottom - r.top)
            except Exception:
                source = None
            for n, (title, element) in enumerate(tabs):
                active = bool(title) and window.title.startswith(title)
                items.append(OverviewItem(key=f"{window.hwnd}:{n}", title=title or "New tab", subtitle=_app_name(exe),
                                          kind="tab", hwnd=window.hwnd, source=source, live=active,
                                          minimized=window.minimized, payload=element))
    return items


def activate(item: OverviewItem) -> bool:
    """Bring the picked window (and, for a tab, that tab) to the front."""
    from app.tools import winapi

    if item.kind == "tab" and item.payload is not None:
        from app.tools import browser_tabs

        browser_tabs.select_tab(item.payload)
    return bool(winapi.force_foreground(item.hwnd))
