"""Small reusable widgets of the command center: the left rail, the compact status area, the global command bar and
the state pill. All of them carry an accessible name, a tooltip and a visible focus ring (see style.py); the state is
always shown with a shape glyph AND text, never by colour alone."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from app.ui.command_center import states

SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("chat", "Chat", "Talk to Jarvis and see what it is doing"),
    ("dashboard", "Dashboard", "Overview of Jarvis, this PC and your devices"),
    ("tasks", "Tasks", "Multi-step tasks as a timeline, with cancel / retry / undo where possible"),
    ("devices", "Devices", "This PC and your paired phone: connection, abilities, permissions"),
    ("memory", "Memory", "What Jarvis remembers, and how to change it"),
    ("knowledge", "Knowledge", "Your documents and notes that Jarvis can search"),
    ("automations", "Automations", "Routines: triggers, conditions, actions, approvals - inspect and test"),
    ("developer", "Developer Tools", "Projects, tests, logs and diagnostics"),
    ("settings", "Settings", "Look and feel, voice, privacy and everything configurable"),
    ("security", "Security", "Paired devices, permissions, microphone and screen state, emergency stop"),
    ("intelligence", "Intelligence", "Goals, decisions, preferences, investigations, briefs, alerts and capability gaps"),
    ("handoff", "Handoff", "Move an unfinished task to your phone and see where each handoff stands"),
    ("gestures", "Gestures", "Control the mouse with your hand: live status, skeleton preview, calibration, tutorial, emergency stop"),
)


class StatePill(QLabel):
    """The current state as a shape + a word (e.g. '▶ Executing'); its colour only reinforces it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statePill")
        self.set_state(states.STATES["idle"])

    def set_state(self, info: states.StateInfo, fg: str = "") -> None:
        """The word keeps the theme's text colour (readable on dark and light); the state colour is the outline."""
        self.info = info
        self.setText(info.text)
        self.setToolTip(info.hint)
        self.setAccessibleName(info.accessible)
        self.setStyleSheet(f"QLabel#statePill {{ color:{fg or info.color}; border:2px solid {info.color}; border-radius:10px;"
                           f" padding:2px 10px; font-weight:600; background:transparent; }}")


class Rail(QFrame):
    """The left navigation rail. Alt+1..9 jump to a section; Tab / arrows move between buttons."""

    selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("rail")
        self.setFixedWidth(200)
        self.setAccessibleName("Navigation")
        self.buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(2)
        title = QLabel("JARVIS")
        title.setObjectName("h1")
        title.setAccessibleName("Jarvis command center")
        layout.addWidget(title)
        layout.addSpacing(8)
        for number, (key, label, tip) in enumerate(SECTIONS, start=1):
            button = QPushButton(f"{label}")
            button.setObjectName("railItem")
            button.setCheckable(True)
            button.setToolTip(f"{tip}  (Alt+{number % 10})" if number <= 10 else tip)
            button.setAccessibleName(f"{label} section")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, k=key: self.select(k))
            self._group.addButton(button)
            self.buttons[key] = button
            layout.addWidget(button)
            if number <= 10:
                QShortcut(QKeySequence(f"Alt+{number % 10}"), self, activated=lambda k=key: self.select(k))
        layout.addStretch(1)
        self.core_button = QPushButton("◐  Core view")
        self.core_button.setToolTip("Back to the 3D core (Ctrl+Shift+H)")
        self.core_button.setAccessibleName("Switch to the 3D core view")
        layout.addWidget(self.core_button)
        self.buttons["chat"].setChecked(True)

    def select(self, key: str) -> None:
        if key in self.buttons:
            self.buttons[key].setChecked(True)
            self.selected.emit(key)

    def current(self) -> str:
        return next((k for k, b in self.buttons.items() if b.isChecked()), "chat")

    def badge(self, key: str, count: int) -> None:
        label = dict((k, name) for k, name, _ in SECTIONS)[key]
        self.buttons[key].setText(f"{label}" + (f"   ({count})" if count else ""))
        self.buttons[key].setAccessibleName(f"{label} section" + (f", {count} need attention" if count else ""))


class StatusArea(QFrame):
    """One compact line: state, local AI model, CPU/RAM/GPU, microphone, network mode, connected devices.

    A figure that hasn't been measured shows '-' (never 0)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusBar")
        self.setAccessibleName("Status")
        self.pill = StatePill()
        self.labels: dict[str, QLabel] = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(18)
        row.addWidget(self.pill)
        for key, tip in (("model", "The local AI model and whether it is reachable"),
                         ("cpu", "CPU load"), ("ram", "Memory in use"), ("gpu", "GPU load and memory"),
                         ("mic", "Microphone state"), ("network", "Network mode"), ("devices", "Connected devices")):
            label = QLabel("-")
            label.setObjectName("muted")
            label.setToolTip(tip)
            label.setAccessibleName(tip)
            self.labels[key] = label
            row.addWidget(label)
        row.addStretch(1)
        self.set_all(model="AI: starting", cpu=None, ram=None, gpu=None, mic="Mic: off", network="Network: -",
                     devices="Devices: -")

    def set_all(self, **values) -> None:
        for key, value in values.items():
            if key not in self.labels:
                continue
            if key in ("cpu", "ram"):
                text = f"{key.upper()} {value:.0f}%" if isinstance(value, (int, float)) else f"{key.upper()} -"
            elif key == "gpu":
                text = f"GPU {value}" if value else "GPU -"
            else:
                text = str(value)
            self.labels[key].setText(text)

    def set_state(self, info: states.StateInfo, fg: str = "") -> None:
        self.pill.set_state(info, fg)


class CommandBar(QFrame):
    """The global command field: type or speak, Enter sends, Esc cancels, Ctrl+L focuses it from anywhere."""

    submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("commandBar")
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask Jarvis or give a command   (Ctrl+L)")
        self.input.setAccessibleName("Command to Jarvis")
        self.input.setClearButtonEnabled(True)
        from PySide6.QtWidgets import QToolButton

        for clear in self.input.findChildren(QToolButton):          # the field's own clear button needs a name too
            clear.setAccessibleName("Clear the command")
            clear.setToolTip("Clear the command")
        self.input.returnPressed.connect(self._send)
        self.mic = QPushButton("🎤 Talk")
        self.mic.setToolTip("Push to talk (Ctrl+Shift+J)")
        self.mic.setAccessibleName("Talk to Jarvis")
        self.send = QPushButton("Send")
        self.send.setObjectName("primary")
        self.send.setToolTip("Send (Enter)")
        self.send.setAccessibleName("Send command")
        self.send.clicked.connect(self._send)
        self.stop = QPushButton("■ Stop")
        self.stop.setToolTip("Stop speaking or cancel the current request (Esc)")
        self.stop.setAccessibleName("Stop the current request")
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setAccessibleName("Execution status")
        self.status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row = QHBoxLayout()
        row.setContentsMargins(8, 6, 8, 6)
        row.addWidget(self.input, 1)
        row.addWidget(self.mic)
        row.addWidget(self.send)
        row.addWidget(self.stop)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(row)
        outer.addWidget(self.status)
        self.status.setContentsMargins(14, 0, 8, 6)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.focus)    # a window-wide shortcut once shown

    def _send(self) -> None:
        text = self.input.text().strip()
        if text:
            self.submitted.emit(text)

    def accepted(self) -> None:
        self.input.clear()

    def focus(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    def show_status(self, info: states.StateInfo, detail: str = "") -> None:
        self.status.setText(f"{info.text}" + (f" - {detail}" if detail else ""))
        self.stop.setEnabled(info.busy or info.key in ("listening",))
