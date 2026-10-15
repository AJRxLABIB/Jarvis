"""Diagnostics & recovery: run Jarvis's own health checks, see the results, and repair its bookkeeping.
Everything is read from what Jarvis recorded; repair never touches your files (app/core/diagnostics.py)."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from app.core import diagnostics

_COLOR = {"ok": "#34d399", "warn": "#f5b642", "fail": "#f87171"}


def _source_root() -> Path | None:
    if getattr(sys, "frozen", False):
        return None
    import app

    return Path(app.__file__).resolve().parent


class DiagnosticsDialog(QDialog):
    def __init__(self, ctx, repair, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Diagnostics & recovery")
        self.setMinimumSize(680, 460)
        self.ctx, self._repair = ctx, repair
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.checks = QListWidget()
        run = QPushButton("Run checks again")
        run.clicked.connect(self.refresh)
        self.repair_button = QPushButton("Repair bookkeeping")
        self.repair_button.clicked.connect(self.do_repair)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(run)
        buttons.addWidget(self.repair_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Jarvis's own health. These checks are read-only; 'Repair' only fixes Jarvis's "
                                "bookkeeping (stuck requests/missions, idle phone sessions, stale caches) - never "
                                "your files."))
        layout.addWidget(self.summary)
        layout.addWidget(self.checks, 1)
        self.result = QLabel()
        self.result.setWordWrap(True)
        layout.addWidget(self.result)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        checks = diagnostics.diagnose(self.ctx, source_root=_source_root())
        self.checks.clear()
        for check in checks:
            item = QListWidgetItem(check.line())
            item.setForeground(Qt.GlobalColor.white)
            color = _COLOR.get(check.status)
            if color:
                from PySide6.QtGui import QColor

                item.setForeground(QColor(color))
            self.checks.addItem(item)
        self.summary.setText(diagnostics.summary(checks).split(" ", 1)[0]
                             + (" — " + f"{sum(1 for c in checks if c.status != 'ok')} need attention"
                                if any(c.status != "ok" for c in checks) else " — all checks passed"))

    def do_repair(self) -> None:
        self.result.setText(self._repair() or "Nothing needed repairing.")
        self.refresh()
