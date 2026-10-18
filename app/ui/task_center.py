"""Background tasks: every mission, scheduled automation, self-improvement test and running request in one
list (app/core/task_center.py). The buttons send ordinary requests ("continue my mission", ...), so each one
still goes through Jarvis's permission and confirmation checks."""

from __future__ import annotations

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.core import task_center


# One colour per state, so running / waiting / paused / failed / completed can be told apart at a glance
STATUS_COLOR = {"RUNNING": "#1565c0", "QUEUED": "#455a64", "WAITING_FOR_APPROVAL": "#6a1b9a", "PAUSED": "#ef6c00", "RECOVERY_REQUIRED": "#d84315",
                "COMPLETED": "#2e7d32", "FAILED": "#c62828", "CANCELLED": "#757575", "ROLLED_BACK": "#757575"}


class TaskCenterDialog(QDialog):
    def __init__(self, database, send_text, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Background tasks")
        self.setMinimumSize(900, 480)
        self.database, self.send_text = database, send_text
        self.tasks: list[task_center.Task] = []
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.finished = QCheckBox("Show finished")
        self.finished.setChecked(True)
        self.finished.toggled.connect(self.refresh)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Status", "Kind", "Task", "Details"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selected)
        self.buttons = QHBoxLayout()
        self.buttons.addWidget(QLabel("Actions (each still asks when it should):"))
        self.buttons.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(self.finished)
        bottom.addStretch(1)
        bottom.addWidget(refresh)
        bottom.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addLayout(self.buttons)
        layout.addLayout(bottom)
        self._action_buttons: list[QPushButton] = []
        self.refresh()

    def refresh(self) -> None:
        self.tasks = task_center.collect(self.database, include_finished=self.finished.isChecked())
        counts = task_center.counts(self.tasks)
        self.summary.setText(", ".join(f"{n} {s.replace('_', ' ').lower()}" for s, n in counts.items())
                             or "Nothing is running or queued in the background.")
        self.table.setRowCount(len(self.tasks))
        for row, t in enumerate(self.tasks):
            for col, value in enumerate((t.status.replace("_", " "), t.kind, t.title, t.detail)):
                cell = QTableWidgetItem(value)
                if col == 0 and t.status in STATUS_COLOR:
                    cell.setForeground(QBrush(QColor(STATUS_COLOR[t.status])))
                    cell.setToolTip({"WAITING_FOR_APPROVAL": "Waiting for your go-ahead",
                                     "PAUSED": "Stopped; say 'continue my mission' or cancel it",
                                     "RECOVERY_REQUIRED": "A step was cut off and may already have happened: "
                                                          "check, then skip it, retry it or cancel"}.get(t.status, ""))
                self.table.setItem(row, col, cell)
        self.table.resizeColumnsToContents()
        self._selected()

    def _selected(self) -> None:
        for button in self._action_buttons:
            self.buttons.removeWidget(button)
            button.deleteLater()
        self._action_buttons = []
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or rows[0].row() >= len(self.tasks):
            return
        for command in self.tasks[rows[0].row()].commands:
            button = QPushButton(command[0].upper() + command[1:])
            button.clicked.connect(lambda _=False, c=command: self._send(c))
            self.buttons.addWidget(button)
            self._action_buttons.append(button)

    def _send(self, command: str) -> None:
        self.send_text(command)
        self.accept()
