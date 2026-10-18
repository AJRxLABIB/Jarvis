"""Security center: one read-only page of facts (see app/core/security_center.py) with buttons to the existing controls."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from app.core import security_center


class SecurityCenterDialog(QDialog):
    def __init__(self, database, settings, *, open_phones: Callable[[], None] | None = None,
                 open_privacy: Callable[[], None] | None = None, open_emergency: Callable[[], None] | None = None,
                 parent: QWidget | None = None, live: Callable[[], dict] | None = None) -> None:
        super().__init__(parent)
        self.database, self.settings = database, settings
        self.live = live          # the running window's real state (microphone, capture, sharing, approvals)
        self.setWindowTitle("Security center")
        self.setMinimumSize(720, 560)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Security summary")
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setAccessibleName("Security report")
        self.buttons: dict[str, QPushButton] = {}
        row = QHBoxLayout()
        for key, label, handler, tip in (
                ("refresh", "&Refresh", self.refresh, "Check everything again (also verifies the activity log)"),
                ("phones", "&Phones...", open_phones, "Trust, restrict or revoke a phone; change what it may do"),
                ("privacy", "P&rivacy...", open_privacy, "What Jarvis stores, and delete it"),
                ("emergency", "&Emergency controls...", open_emergency, "Stop everything now, or cut off network / control")):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.setEnabled(handler is not None)
            if handler is not None:
                button.clicked.connect(lambda _=False, h=handler: h())
            self.buttons[key] = button
            row.addWidget(button)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.report, 1)
        layout.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        try:
            live = self.live() if self.live is not None else None
        except Exception:
            live = None
        sections = security_center.collect(self.database, self.settings, live=live)
        self.report.setPlainText(security_center.describe(sections))
        warns = sum(1 for s in sections for i in s.items if i.level == "warn")
        self.summary.setText(f"{warns} item(s) need a look." if warns else "Nothing needs attention right now.")
