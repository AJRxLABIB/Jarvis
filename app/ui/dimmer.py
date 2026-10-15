"""Software screen dimming, for monitors that don't take brightness commands (no DDC/CI, some TVs, some
docks). A black, click-through, always-on-top veil over every screen; its opacity makes the picture darker.
It can only make the screen darker than the monitor's own setting, never brighter - the tool says so.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

MAX_OPACITY = 0.85  # 0% still leaves the screen readable enough to turn it back up


def opacity_for(level: int) -> float:
    """Brightness percent (100 = no veil) -> veil opacity."""
    level = max(0, min(100, int(level)))
    return round((100 - level) / 100 * MAX_OPACITY, 3)


class ScreenDimmer:
    def __init__(self) -> None:
        self._veils: list[QWidget] = []
        self.level = 100

    def set_level(self, level: int) -> int:
        self.level = max(0, min(100, int(level)))
        self._clear()
        if self.level >= 100:
            return self.level
        for screen in QGuiApplication.screens():
            veil = QWidget(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                           | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput
                           | Qt.WindowType.WindowDoesNotAcceptFocus)
            veil.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            veil.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            veil.setStyleSheet("background: black;")
            veil.setGeometry(screen.geometry())
            veil.setWindowOpacity(opacity_for(self.level))
            veil.show()
            self._veils.append(veil)
        return self.level

    def _clear(self) -> None:
        for veil in self._veils:
            veil.close()
            veil.deleteLater()
        self._veils = []
