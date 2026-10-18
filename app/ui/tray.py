"""System tray icon and menu. Every entry does something real."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app.ui.styles.theme import STATE_COLORS
from app.ui.styles.tokens import COLOR


def make_icon(state: str = "IDLE", capturing: bool = False) -> QIcon:
    """An original round icon drawn in code (no external assets). A red ring = screen capture active."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(STATE_COLORS.get(state, STATE_COLORS["IDLE"]))
    gradient = QRadialGradient(24, 24, 36)
    gradient.setColorAt(0.0, QColor(255, 255, 255))
    gradient.setColorAt(0.35, color.lighter(120))
    gradient.setColorAt(1.0, color.darker(250))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(4, 4, 56, 56)
    if capturing:
        from PySide6.QtGui import QPen

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(COLOR["danger"]), 7))
        painter.drawEllipse(5, 5, 54, 54)
    painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    def __init__(self, window, controller, open_logs, quit_app) -> None:
        super().__init__(window)
        self.window = window
        self.controller = controller
        self.tray = QSystemTrayIcon(make_icon(), window)
        self.tray.setToolTip(controller.ctx.settings.assistant_name)
        menu = QMenu()
        self.actions: dict[str, QAction] = {}

        def add(key: str, text: str, slot, checkable: bool = False) -> QAction:
            action = QAction(text, menu)
            action.setCheckable(checkable)
            action.triggered.connect(slot)
            menu.addAction(action)
            self.actions[key] = action
            return action

        add("open", "Open Assistant", self.show_window)
        menu.addSeparator()
        hands_free = add("hands_free", "Always Listening (hands-free)",
                         lambda checked: controller.set_hands_free(checked), checkable=True)
        hands_free.setChecked(controller.ctx.settings.hands_free)
        controller.hands_free_state.connect(
            lambda state: (hands_free.blockSignals(True), hands_free.setChecked(controller.hands_free is not None),
                           hands_free.blockSignals(False)))
        add("listen", "Start Listening", controller.start_listening)
        add("stop_listen", "Stop Listening", controller.stop_listening)
        add("mute", "Mute Voice", lambda checked: controller.set_muted(checked), checkable=True)
        mic = add("mute_mic", "Mute Microphone", lambda checked: controller.set_mic_muted(checked), checkable=True)
        if hasattr(controller, "mic_muted_changed"):
            controller.mic_muted_changed.connect(
                lambda muted: (mic.blockSignals(True), mic.setChecked(muted), mic.blockSignals(False)))
        add("new_chat", "New Conversation", self._new_conversation)
        menu.addSeparator()
        add("settings", "Settings...", lambda: (self.show_window(), window.open_settings()))
        add("memory", "Memory...", lambda: (self.show_window(), window.open_memory()))
        add("automations", "Automations...", lambda: (self.show_window(), window.open_automations()))
        add("logs", "View Logs", open_logs)
        add("exit", "Exit", quit_app)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self._state, self._capturing = "IDLE", False
        controller.state_changed.connect(self._on_state)
        controller.screen_capture_active.connect(self._on_capture)
        controller.reminder.connect(self._reminder)
        window.hidden_to_tray.connect(self._hint_once)
        self._hinted = False

    def _on_state(self, state: str) -> None:
        self._state = state
        self.tray.setIcon(make_icon(state, self._capturing))

    def _on_capture(self, active: bool) -> None:
        """Visible even when the window is hidden in the tray."""
        self._capturing = active
        self.tray.setIcon(make_icon(self._state, active))
        name = self.controller.ctx.settings.assistant_name
        self.tray.setToolTip(f"{name} - SCREEN CAPTURE ACTIVE" if active else name)

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def show_window(self) -> None:
        bring_back = getattr(self.window, "bring_back", None)
        if callable(bring_back):
            bring_back()  # full screen stays full screen
            return
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _new_conversation(self) -> None:
        self.window.new_conversation()

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.window.isVisible() and not self.window.isMinimized():
                self.window.hide()
            else:
                self.show_window()

    def _reminder(self, title: str, message: str) -> None:
        if self.tray.isVisible():
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 10_000)

    def _hint_once(self) -> None:
        if not self._hinted and self.tray.isVisible():
            self._hinted = True
            self.tray.showMessage(self.controller.ctx.settings.assistant_name,
                                  "Still running in the tray. Right-click the icon to exit.",
                                  QSystemTrayIcon.MessageIcon.Information, 3000)
