"""The confirmation dialog. CANCEL is the default button (Enter cancels), the
dialog times out to CANCEL, and only an explicit click on CONFIRM approves."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.security.confirmations import ConfirmationRequest
from app.ui.styles.tokens import COLOR

_LEVEL_COLORS = {"safe": COLOR["accent"], "moderate": COLOR["warn"], "dangerous": COLOR["danger"], "critical": "#ef4444"}


class ConfirmDialog(QDialog):
    def __init__(self, request: ConfirmationRequest, heard: str | None = None, timeout: int = 60,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm action")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(460)
        self._remaining = timeout

        level = request.level_name
        badge = QLabel(level.upper())
        badge.setObjectName("badge")
        badge.setStyleSheet(f"background:{_LEVEL_COLORS.get(level, '#94a3b8')}; color:#0b1020;")
        title = QLabel("The assistant wants to do this:")
        title.setObjectName("title")
        summary = QLabel(request.summary)
        summary.setWordWrap(True)
        summary.setTextFormat(Qt.TextFormat.PlainText)  # never render model text as rich text
        summary.setStyleSheet("font-size: 11pt; color: #f1f5f9; padding: 6px 0;")

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(title)
        top.addStretch()
        top.addWidget(badge)
        layout.addLayout(top)
        layout.addWidget(summary)
        if heard:
            heard_label = QLabel(f'You said: "{heard}"')
            heard_label.setTextFormat(Qt.TextFormat.PlainText)
            heard_label.setWordWrap(True)
            heard_label.setObjectName("muted")
            layout.addWidget(heard_label)
        tool = QLabel(f"Tool: {request.tool}")
        tool.setObjectName("muted")
        tool.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(tool)

        self.countdown = QLabel()
        self.countdown.setObjectName("muted")
        self.cancel_button = QPushButton("CANCEL")
        self.confirm_button = QPushButton("CONFIRM")
        self.confirm_button.setObjectName("danger" if level in ("dangerous", "critical") else "primary")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.confirm_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.countdown)
        buttons.addStretch()
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick(first=True)
        self.cancel_button.setFocus()

    def _tick(self, first: bool = False) -> None:
        if not first:
            self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self.reject()
            return
        self.countdown.setText(f"Cancels automatically in {self._remaining} s")
