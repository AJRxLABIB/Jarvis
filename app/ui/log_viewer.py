"""Log viewer: the newest entries of the JSON log, filterable by level and text.
Entries were redacted before they were written, so no secrets are shown."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MAX_LINES = 3000


def read_log(path: Path, max_lines: int = MAX_LINES) -> list[dict]:
    """Newest-first log records. Unreadable lines are skipped, never fatal."""
    if not path.is_file():
        return []
    with open(path, encoding="utf-8", errors="replace") as handle:
        tail = deque(handle, maxlen=max_lines)
    records = []
    for line in reversed(tail):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def filter_records(records: list[dict], min_level: str = "INFO", text: str = "") -> list[dict]:
    threshold = LEVELS.index(min_level) if min_level in LEVELS else 0
    needle = text.lower().strip()
    out = []
    for record in records:
        level = record.get("level", "INFO")
        if level in LEVELS and LEVELS.index(level) < threshold:
            continue
        if needle and needle not in json.dumps(record, ensure_ascii=False).lower():
            continue
        out.append(record)
    return out


class LogViewer(QDialog):
    def __init__(self, log_file: Path, open_file=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Logs")
        self.setMinimumSize(900, 520)
        self.log_file = log_file
        self._records: list[dict] = []

        self.level = QComboBox()
        self.level.addItems(LEVELS)
        self.level.setCurrentText("INFO")
        self.level.currentTextChanged.connect(self.apply_filter)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter (e.g. tool_call, error, chrome)...")
        self.search.textChanged.connect(self.apply_filter)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload)
        open_button = QPushButton("Open log file")
        open_button.clicked.connect(lambda: open_file(str(self.log_file)) if open_file else None)
        open_button.setEnabled(open_file is not None)
        top = QHBoxLayout()
        top.addWidget(QLabel("Level"))
        top.addWidget(self.level)
        top.addWidget(self.search, 1)
        top.addWidget(refresh)
        top.addWidget(open_button)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "Level", "Source", "Event", "Details"])
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.count = QLabel()
        self.count.setObjectName("muted")
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.count)
        self.reload()

    def reload(self) -> None:
        self._records = read_log(self.log_file)
        self.apply_filter()

    def apply_filter(self) -> None:
        rows = filter_records(self._records, self.level.currentText(), self.search.text())[:1000]
        self.table.setRowCount(len(rows))
        for row, record in enumerate(rows):
            fields = dict(record.get("fields") or {})
            fields.pop("event", None)
            details = json.dumps(fields, ensure_ascii=False, default=str) if fields else ""
            if record.get("exc"):
                details = (details + " " + record["exc"].splitlines()[-1]).strip()
            values = [record.get("ts", "")[11:19], record.get("level", ""), record.get("logger", "").removeprefix("jarvis."),
                      record.get("msg", ""), details]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self.table.resizeColumnsToContents()
        self.count.setText(f"{len(rows)} of {len(self._records)} recent entries ({self.log_file})")
