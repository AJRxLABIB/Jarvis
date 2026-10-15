"""The mini orb: a small always-on-top reactor in the corner of the screen (MINI_ORB).

It appears while the main window is hidden or minimised and shows what Jarvis is doing
(listening, thinking, speaking) at a glance. Drag it anywhere; double-click (or say
"Jarvis") to open the main window; right-click for a small menu.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu, QVBoxLayout, QWidget

from app.ui.visualizer import OrbWidget

SIZE = 64  # default; MINI_ORB_SIZE changes it


class MiniOrb(QWidget):
    open_requested = Signal()
    hide_requested = Signal()

    def __init__(self, size: int = SIZE) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.size_px = size
        self.setFixedSize(size, size)
        self.setToolTip("Jarvis - double-click to open, drag to move")
        self.orb = OrbWidget(compact=True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.orb)
        self._drag: QPoint | None = None
        self.move_to_corner()

    def pop_in(self) -> None:
        """Appear with a quick fade (when the main window steps aside)."""
        from app.ui import motion

        if not motion.ENABLED:
            self.show()
            return
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation

        self.setWindowOpacity(0.0)
        self.show()
        self._pop = QPropertyAnimation(self, b"windowOpacity", self)
        self._pop.setDuration(350)
        self._pop.setStartValue(0.0)
        self._pop.setEndValue(1.0)
        self._pop.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pop.start()

    def move_to_corner(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.size_px - 16, area.bottom() - self.size_px - 16)

    def set_state(self, state: str) -> None:
        self.orb.set_state(state)

    def set_level(self, dbfs: float) -> None:
        self.orb.set_level(dbfs)

    # ---------------------------------------------------------------- mouse

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.open_requested.emit()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.addAction("Open Jarvis", self.open_requested.emit)
        menu.addAction("Hide the mini orb", self.hide_requested.emit)
        menu.exec(event.globalPos())
