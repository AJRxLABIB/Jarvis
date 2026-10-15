"""Mission Control: what Jarvis did, request by request (the agent trace), and what it can undo.

Top: your recent requests (runs) - when, from where (voice, typed, phone, automation), what you asked
(redacted), how it ended, how long it took, which tools ran. Below: the selected request's trace - every
check and step the executor recorded (permission level, decision, confirmation, verification, result).
Right: actions that can be undone, newest first. Everything is read from the local database.
"""

from __future__ import annotations

import json
from datetime import datetime

from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QListWidget, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt

from app.core.runs import RunStore
from app.core.undo import UndoStore

LEVELS = {0: "safe", 1: "moderate", 2: "dangerous", 3: "critical"}


def _time(text: str | None) -> str:
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).astimezone().strftime("%d %b %H:%M:%S")
    except ValueError:
        return text[:19]


def trace_rows(trace: list[dict]) -> list[list[str]]:
    rows = []
    for entry in trace:
        try:
            details = json.loads(entry.get("details") or "{}")
        except ValueError:
            details = {}
        note = details.get("message") or details.get("reason") or details.get("found") or ""
        if "ok" in details and entry["event"] == "tool_result":
            note = ("ok: " if details.get("ok") else f"failed ({details.get('error')}): ") + str(note)
        level = entry.get("permission_level")
        rows.append([_time(entry.get("ts")), entry.get("event", ""), entry.get("tool") or "",
                     LEVELS.get(level, "") if level is not None else "", entry.get("decision") or "", str(note)[:160]])
    return rows


class MissionControlDialog(QDialog):
    def __init__(self, database, on_undo=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mission Control")
        self.setMinimumSize(1000, 560)
        self.runs, self.undo = RunStore(database), UndoStore(database)
        self.on_undo = on_undo

        self.run_table = self._table(["When", "From", "Request", "Result", "ms", "Tools"])
        self.run_table.itemSelectionChanged.connect(self.show_trace)
        self.trace_table = self._table(["When", "Step", "Tool", "Level", "Decision", "Detail"])
        self.undo_list = QListWidget()
        undo_button = QPushButton("Undo the newest")
        undo_button.setEnabled(on_undo is not None)
        undo_button.clicked.connect(self._undo)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)

        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(self.run_table)
        left.addWidget(self.trace_table)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Can be undone (newest first)"))
        right_layout.addWidget(self.undo_list, 1)
        right_layout.addWidget(undo_button)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([760, 240])
        buttons = QHBoxLayout()
        buttons.addWidget(refresh)
        buttons.addStretch()
        buttons.addWidget(close)
        hint = QLabel("Every request and every step Jarvis took for it: permission level, your answers, checks and "
                      "results. Request text is stored redacted and shortened.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        self.goal_list, self.mission_list = QListWidget(), QListWidget()
        plans = QWidget()
        plans_layout = QHBoxLayout(plans)
        for title, widget in (("Goals", self.goal_list), ("Missions (newest first)", self.mission_list)):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            column.addWidget(widget, 1)
            plans_layout.addLayout(column, 1)
        self.tabs = QTabWidget()
        self.tabs.addTab(split, "Requests and trace")
        self.tabs.addTab(plans, "Goals and missions")
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(buttons)
        self.database = database
        self.refresh()

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        return table

    def refresh(self) -> None:
        runs = self.runs.recent(50)
        self.run_table.setRowCount(len(runs))
        for row, run in enumerate(runs):
            values = [_time(run["started_at"]), run["source"], run["text"], run["state"],
                      str(run["elapsed_ms"] or ""), run["tools"]]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, run["id"])
                self.run_table.setItem(row, col, item)
        self.run_table.resizeColumnsToContents()
        self.undo_list.clear()
        for record in self.undo.pending(10):
            self.undo_list.addItem(f"{_time(record['created_at'])}  {record['description']}")
        if runs and not self.run_table.selectedItems():
            self.run_table.selectRow(0)
        self._refresh_plans()

    def _refresh_plans(self) -> None:
        from datetime import date

        from app.core.goals import GoalStore
        from app.core.missions import MissionStore

        database = getattr(self, "database", None)
        if database is None:
            return
        self.goal_list.clear()
        for goal in GoalStore(database).active():
            self.goal_list.addItem(goal.describe(date.today()))
            for task in goal.tasks:
                self.goal_list.addItem(f"    {'[x]' if task['done'] else '[ ]'} {task['text']}")
        self.mission_list.clear()
        for mission in MissionStore(database).recent(10):
            self.mission_list.addItem(f"{mission.title} - {mission.state}, {mission.progress()} steps"
                                      + (f" ({mission.note})" if mission.note else ""))
            for step in mission.steps:
                self.mission_list.addItem(f"    {step.n}. {step.title}: {step.state}")

    def show_trace(self) -> None:
        items = self.run_table.selectedItems()
        if not items:
            return
        rows = trace_rows(self.runs.trace(items[0].data(Qt.ItemDataRole.UserRole)))
        self.trace_table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                self.trace_table.setItem(r, c, QTableWidgetItem(value))
        self.trace_table.resizeColumnsToContents()

    def _undo(self) -> None:
        if self.on_undo is not None:
            self.on_undo()
            self.accept()
