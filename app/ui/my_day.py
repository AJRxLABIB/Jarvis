"""My day: today's overview, your to-do list and what happened to your missions - in one window.

Three tabs, all read from the local database through the same functions the voice tools use
(app/core/command_center.py, app/tools/productivity.py, app/core/missions.py):

* Today   - overdue / due today / blocked / waiting for you / what you finished (no calendar is assumed).
* To-dos  - add, complete, postpone, change priority, delete; repeating items and "after" dependencies.
* History - the state changes of each mission and why (started, paused, verified, completed ...).

Keyboard: Alt+A add, Alt+C complete, Alt+P postpone, Alt+D delete, Alt+R refresh, Tab moves between the controls and
Esc closes. Your own clicks change your own list directly; nothing here lets a model act without the usual checks.
"""

from __future__ import annotations

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

from app.core import command_center
from app.core.missions import MissionStore
from app.tools import productivity
from app.tools.base import ToolError

PRIORITY_LABEL = {1: "High", 2: "Normal", 3: "Low"}
STATE_COLOR = {"completed": "#2e7d32", "failed": "#c62828", "paused": "#ef6c00", "running": "#1565c0",
               "verifying": "#6a1b9a", "cancelled": "#757575", "rolled_back": "#757575", "planned": "#455a64"}


class AddTodoDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a to-do")
        self.text = QLineEdit()
        self.text.setPlaceholderText("What needs doing?")
        self.due = QLineEdit()
        self.due.setPlaceholderText("today, tomorrow, friday or 2026-10-20 (optional)")
        self.priority = QComboBox()
        self.priority.addItems(["normal", "high", "low"])
        self.category = QLineEdit()
        self.category.setPlaceholderText("optional, e.g. work")
        self.repeat = QComboBox()
        self.repeat.addItems(["", "daily", "weekdays", "weekly", "monthly"])
        self.repeat.setToolTip("A repeating item needs a due day")
        self.after = QLineEdit()
        self.after.setPlaceholderText("optional: words of another item that must be done first")
        self.minutes = QLineEdit()
        self.minutes.setPlaceholderText("optional: estimated minutes, for the day plan")
        form = QFormLayout()
        for label, widget in (("&Text", self.text), ("&Due", self.due), ("&Priority", self.priority),
                              ("&Category", self.category), ("&Repeat", self.repeat), ("&After", self.after),
                              ("&Minutes", self.minutes)):
            form.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.text.setFocus()

    def values(self) -> dict:
        return {"text": self.text.text().strip(), "due": self.due.text().strip() or None,
                "priority": self.priority.currentText(), "category": self.category.text().strip(),
                "repeat": self.repeat.currentText(), "after": self.after.text().strip() or None,
                "minutes": int(self.minutes.text()) if self.minutes.text().strip().isdigit() else None}


class MyDayDialog(QDialog):
    def __init__(self, ctx, send_text=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx, self.send_text = ctx, send_text
        self.setWindowTitle("My day")
        self.setMinimumSize(980, 560)
        self.items: list[dict] = []
        self.missions: list = []

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Status message")

        # Today
        self.today_text = QPlainTextEdit()
        self.today_text.setReadOnly(True)
        self.today_text.setAccessibleName("Today overview")
        self.evening = QPushButton("&Evening summary")
        self.evening.setCheckable(True)
        self.evening.toggled.connect(self.refresh)
        self.plan_button = QPushButton("&Plan my day")
        self.plan_button.setToolTip("Order today's to-dos by urgency and time; changes nothing on your list or calendar")
        self.plan_button.clicked.connect(lambda: self._show_plan(False))
        self.review_button = QPushButton("&Review plan")
        self.review_button.setToolTip("Compare today's plan with what is really done")
        self.review_button.clicked.connect(lambda: self._show_plan(True))
        today = QWidget()
        today_layout = QVBoxLayout(today)
        today_layout.addWidget(self.today_text, 1)
        plan_row = QHBoxLayout()
        plan_row.addWidget(self.evening)
        plan_row.addWidget(self.plan_button)
        plan_row.addWidget(self.review_button)
        plan_row.addStretch(1)
        today_layout.addLayout(plan_row)

        # To-dos
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Priority", "To-do", "Due", "Category", "Repeats", "Waiting for", "Minutes"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setAccessibleName("To-do list")
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.add_button = QPushButton("&Add...")
        self.add_button.clicked.connect(self.add_todo)
        self.done_button = QPushButton("&Complete")
        self.done_button.clicked.connect(self.complete_selected)
        self.postpone_button = QPushButton("&Postpone")
        self.postpone_button.setToolTip("Move it to the next day")
        self.postpone_button.clicked.connect(self.postpone_selected)
        self.priority_button = QPushButton("Pr&iority")
        self.priority_button.setToolTip("Cycle high / normal / low")
        self.priority_button.clicked.connect(self.cycle_priority)
        self.delete_button = QPushButton("&Delete")
        self.delete_button.clicked.connect(self.delete_selected)
        self.row_buttons = (self.done_button, self.postpone_button, self.priority_button, self.delete_button)
        buttons = QHBoxLayout()
        buttons.addWidget(self.add_button)
        for button in self.row_buttons:
            buttons.addWidget(button)
        buttons.addStretch(1)
        todos = QWidget()
        todos_layout = QVBoxLayout(todos)
        self.empty_label = QLabel("Your list is empty. Press Add to start.")
        todos_layout.addWidget(self.empty_label)
        todos_layout.addWidget(self.table, 1)
        todos_layout.addLayout(buttons)

        # History
        self.mission_list = QListWidget()
        self.mission_list.setAccessibleName("Missions")
        self.mission_list.currentRowChanged.connect(self._show_history)
        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setAccessibleName("Mission history")
        history = QWidget()
        history_layout = QHBoxLayout(history)
        history_layout.addWidget(self.mission_list, 1)
        history_layout.addWidget(self.history, 2)

        self.tabs = QTabWidget()
        self.tabs.addTab(today, "&Today")
        self.tabs.addTab(todos, "To-d&os")
        self.tabs.addTab(history, "&History")

        refresh = QPushButton("&Refresh")
        refresh.clicked.connect(self.refresh)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(refresh)
        bottom.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(bottom)
        self.refresh()

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        try:
            data = command_center.snapshot(self.ctx)
            self.today_text.setPlainText(self._today_text(data))
        except Exception as exc:  # a broken source shows what failed instead of an empty window
            self.today_text.setPlainText(f"Couldn't read today's overview: {exc}")
        try:
            self.items = productivity.open_todos(self.ctx)
        except ToolError as exc:
            self.items = []
            self._say(str(exc), error=True)
        self._fill_table()
        self._fill_missions()
        self._selection_changed()

    def _show_plan(self, review: bool) -> None:
        """Plan (or review) today with the same function the voice tool uses; the answer replaces the overview text."""
        try:
            result = productivity.plan_my_day(productivity.PlanArgs(review=review), self.ctx)
            self.today_text.setPlainText(result.message)
            self._say("Review of today's plan." if review else "Plan made. Add minutes to your to-dos to make it more exact.")
        except ToolError as exc:
            self._say(str(exc), error=True)

    def _today_text(self, data: dict) -> str:
        lines = [command_center.describe(data, evening=self.evening.isChecked()), ""]
        todos = data.get("todos")
        if todos:
            for title, key in (("Overdue", "overdue"), ("Due today", "due_today")):
                if todos[key]:
                    lines.append(f"{title}:")
                    lines += [f"  - {text}" for text in todos[key]]
            if todos["blocked"]:
                lines.append("Blocked:")
                lines += [f"  - {b['item']} (waiting for {b['waiting_for']})" for b in todos["blocked"]]
        if data.get("waiting_for_you"):
            lines.append("Waiting for you:")
            lines += [f"  - {t}" for t in data["waiting_for_you"]]
        done = (data.get("today") or {}).get("todos_done") or []
        if done:
            lines.append("Finished today:")
            lines += [f"  - {t}" for t in done]
        return "\n".join(lines)

    def _fill_table(self) -> None:
        self.table.setRowCount(len(self.items))
        self.empty_label.setVisible(not self.items)
        self.table.setVisible(bool(self.items))
        today = self.ctx.clock().date().isoformat()
        for row, item in enumerate(self.items):
            cells = (PRIORITY_LABEL.get(item["priority"], "Normal"), item["text"], item["due"] or "",
                     item["category"], item["repeat"], item["blocked_by"] or "",
                     str(item["minutes"]) if item.get("minutes") else "?")
            for col, value in enumerate(cells):
                cell = QTableWidgetItem(value)
                if item["due"] and item["due"] < today:
                    cell.setForeground(QBrush(QColor("#c62828")))  # overdue
                elif item["blocked_by"]:
                    cell.setForeground(QBrush(QColor("#757575")))
                self.table.setItem(row, col, cell)
        self.table.resizeColumnsToContents()

    def _fill_missions(self) -> None:
        self.mission_list.clear()
        try:
            self.missions = MissionStore(self.ctx.database).recent(20)
        except Exception:
            self.missions = []
        for mission in self.missions:
            entry = f"{mission.title}  [{mission.state.replace('_', ' ')}]  {mission.progress()} steps"
            from PySide6.QtWidgets import QListWidgetItem

            row = QListWidgetItem(entry)
            row.setForeground(QBrush(QColor(STATE_COLOR.get(mission.state, "#000000"))))
            self.mission_list.addItem(row)
        if not self.missions:
            self.history.setPlainText("No missions yet. Ask Jarvis to start one, for example "
                                      "\"start a mission to prepare tomorrow's presentation\".")
        elif self.mission_list.currentRow() < 0:
            self.mission_list.setCurrentRow(0)

    def _show_history(self, row: int) -> None:
        if not 0 <= row < len(self.missions):
            return
        mission = self.missions[row]
        events = MissionStore(self.ctx.database).history(mission.id)
        lines = [f"{mission.title} - {mission.state.replace('_', ' ')}", ""]
        for step in mission.steps:
            lines.append(f"  step {step.n}: {step.title} - {step.state}" + (f" ({step.result[:80]})" if step.result else ""))
        lines.append("")
        if not events:
            lines.append("No recorded state changes yet.")
        for event in events:
            when = (event["ts"] or "")[11:19]
            note = f" - {event['note']}" if event["note"] else ""
            lines.append(f"{when}  {event['old_state'] or 'new'} -> {event['new_state']}{note}")
        self.history.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------ actions

    def _selected_item(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or rows[0].row() >= len(self.items):
            return None
        return self.items[rows[0].row()]

    def _selection_changed(self) -> None:
        has = self._selected_item() is not None
        for button in self.row_buttons:
            button.setEnabled(has)

    def _say(self, message: str, *, error: bool = False) -> None:
        self.status.setText(message)
        self.status.setStyleSheet("color: #c62828;" if error else "")

    def _run(self, action, *args) -> bool:
        try:
            result = action(*args)
        except ToolError as exc:
            self._say(str(exc), error=True)
            return False
        self._say(result.message)
        self.refresh()
        return True

    def add_todo(self) -> None:
        dialog = AddTodoDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values["text"]:
            self._say("Write what needs doing first.", error=True)
            return
        try:
            args = productivity.TodoArgs(**values)
        except ValueError as exc:
            self._say(f"That can't be added: {exc}", error=True)
            return
        self._run(productivity.add_todo, args, self.ctx)

    def complete_selected(self) -> None:
        if (item := self._selected_item()) is not None:
            self._run(productivity.complete_todo, productivity.TodoItemArgs(item=f"id:{item['id']}"), self.ctx)

    def postpone_selected(self) -> None:
        if (item := self._selected_item()) is not None:
            self._run(productivity.update_todo,
                      productivity.UpdateTodoArgs(item=f"id:{item['id']}", action="postpone"), self.ctx)

    def cycle_priority(self) -> None:
        if (item := self._selected_item()) is not None:
            order = ["high", "normal", "low"]
            new = order[(item["priority"]) % 3]  # 1 high -> normal, 2 normal -> low, 3 low -> high
            self._run(productivity.update_todo,
                      productivity.UpdateTodoArgs(item=f"id:{item['id']}", action="set_priority", priority=new),
                      self.ctx)

    def delete_selected(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        answer = QMessageBox.question(self, "Delete", f"Remove '{item['text']}' without doing it?")
        if answer == QMessageBox.StandardButton.Yes:
            self._run(productivity.delete_todo, productivity.TodoItemArgs(item=f"id:{item['id']}"), self.ctx)
