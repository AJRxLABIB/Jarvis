"""Jarvis's own logo for the taskbar, window and Alt+Tab (the header's three-ring reactor mark on a dark disc), and the
Windows app identity that makes the taskbar show THAT - not the Python interpreter's icon and name.

Without an explicit AppUserModelID, Windows groups Jarvis's window with pythonw.exe and shows Python's icon (in the
screenshot: the IDLE / Python icon). With its own ID the window gets its own taskbar button with this logo.
"""

from __future__ import annotations

import sys

APP_ID = "Labib.Jarvis.Assistant"
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def set_app_id() -> bool:
    """Give this process its own taskbar identity (Windows only; call before any window exists)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        return True
    except Exception:
        return False


def draw_logo(size: int):
    """The logo as a QPixmap of ``size`` px: dark disc, cyan / violet / gold rings, a glowing centre."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QRadialGradient

    from app.ui.styles.tokens import COLOR

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 36.0
    disc = QRadialGradient(size / 2, size / 2, size / 2)
    disc.setColorAt(0.0, QColor(14, 34, 58))
    disc.setColorAt(0.85, QColor(5, 12, 24))
    disc.setColorAt(1.0, QColor(5, 12, 24, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(disc)
    p.drawEllipse(QRectF(0, 0, size, size))
    r = QRectF(3 * s, 3 * s, 30 * s, 30 * s)
    thick = max(1.2, 2.6 * s)
    for rect, color, width, start, gap in ((r, COLOR["cyan"], thick, 40, 50),
                                           (r.adjusted(6 * s, 6 * s, -6 * s, -6 * s), COLOR["violet"], thick, 200, 90),
                                           (r.adjusted(11 * s, 11 * s, -11 * s, -11 * s), COLOR["warn"], thick * 0.8, 300, 120)):
        pen = QPen(QColor(color), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(rect, int(start * 16), int((360 - gap) * 16))
    core = QRadialGradient(size / 2, size / 2, 5 * s)
    core.setColorAt(0.0, QColor(255, 255, 255, 255))
    core.setColorAt(0.5, QColor(125, 211, 252, 200))
    core.setColorAt(1.0, QColor(56, 189, 248, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(core)
    p.drawEllipse(QRectF(size / 2 - 5 * s, size / 2 - 5 * s, 10 * s, 10 * s))
    p.end()
    return pixmap


def app_icon():
    """A QIcon with every size Windows asks for (crisp in the taskbar, Alt+Tab and the title bar)."""
    from PySide6.QtGui import QIcon

    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(draw_logo(size))
    return icon
