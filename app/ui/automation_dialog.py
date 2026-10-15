"""Automations window: reminders, schedules and workflows - pause, resume, run now, delete."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.automation.tasks import AutomationStore


class AutomationDialog(QDialog):
    def __init__(self, store: AutomationStore, controller=None, parent: QWidget | None = None, ask=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Automations")
        self.setMinimumSize(820, 420)
        self.store = store
        self.controller = controller
        self._ask = ask or (lambda title, text: QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "When", "Steps", "Next run", "Last result"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)

        self.toggle_button = QPushButton("Pause / Resume")
        self.toggle_button.clicked.connect(self.toggle_selected)
        self.run_button = QPushButton("Run now")
        self.run_button.clicked.connect(self.run_selected)
        self.run_button.setEnabled(controller is not None)
        self.new_button = QPushButton("New voice command...")
        self.new_button.clicked.connect(self.new_command)
        self.new_button.setEnabled(controller is not None and getattr(controller, "ctx", None) is not None)
        delete = QPushButton("Delete")
        delete.setObjectName("danger")
        delete.clicked.connect(self.delete_selected)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        for button in (self.new_button, self.toggle_button, self.run_button, delete):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(close)

        hint = QLabel('Create them by asking, e.g. "Remind me at 11 PM to shut down" or '
                      '"Every weekday at 8 AM open Chrome and VS Code", or press "New voice command" to make your '
                      'own ("gaming time" -> close Chrome, open Steam, volume 70). They run while the assistant is open.')
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        tasks = self.store.all()
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            next_run = ""
            if task.next_run_at and task.enabled:
                next_run = datetime.fromisoformat(task.next_run_at).strftime("%a %d %b %H:%M")
            values = [task.name + ("" if task.enabled else "  (paused)"), task.trigger.describe(),
                      "; ".join(s.describe() for s in task.steps), next_run, task.last_status or ""]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, task.id)
                self.table.setItem(row, col, item)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)

    def new_command(self) -> None:
        from app.ui.command_builder import CommandBuilderDialog

        dialog = CommandBuilderDialog(self.controller.ctx, self)
        if dialog.exec() and dialog.saved is not None:
            self.refresh()

    def selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.store.get(self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole))

    def toggle_selected(self) -> None:
        task = self.selected()
        if task is None:
            return
        next_run = None if task.enabled else task.trigger.next_run(datetime.now().astimezone())
        self.store.set_enabled(task.id, not task.enabled, next_run)
        self.refresh()

    def run_selected(self) -> None:
        task = self.selected()
        if task is None or self.controller is None or self.controller.scheduler is None:
            return
        self.controller.runner.submit(self.controller.scheduler.run(task, manual=True))
        self.accept()  # results arrive in the chat

    def delete_selected(self) -> None:
        task = self.selected()
        if task is not None and self._ask("Delete", f"Delete the automation '{task.name}'?"):
            self.store.delete(task.id)
            self.refresh()
