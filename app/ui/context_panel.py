"""Context panel: what Jarvis understands about what you're doing right now, and how it knows each part."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, \
    QWidget

from app.core import context_engine


class ContextPanel(QDialog):
    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("What Jarvis sees")
        self.setMinimumSize(760, 460)
        self.ctx = ctx
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["What", "Value", "How sure", "Where it comes from"])
        self.table.horizontalHeader().setStretchLastSection(True)
        note = QLabel("Read from the window you're in (its title, never the screen), your projects, git, goals, "
                      "missions and this PC. Nothing here is saved: the last snapshots are kept in memory for 15 "
                      "minutes. Private mode or Settings -> Privacy turns window titles off.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(refresh)
        buttons.addStretch()
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addWidget(note)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        snap = context_engine.ENGINE.snapshot(self.ctx)
        self.summary.setText(f"{snap.line() or 'Nothing known right now.'}   (coverage {int(snap.confidence * 100)}%)")
        self.table.setRowCount(0)
        for name, fact in snap.facts.items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            value = fact.value if fact.certainty != context_engine.UNKNOWN else "-"
            if isinstance(value, (list, dict)):
                value = ", ".join(value) if isinstance(value, list) else \
                    ", ".join(f"{k} {v}" for k, v in value.items())
            for column, text in enumerate((name.replace("_", " "), str(value), fact.certainty, fact.source)):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
