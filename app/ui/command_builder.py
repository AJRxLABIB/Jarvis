"""'New voice command' form: a phrase and a few steps, no code (see app/automation/builder.py)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from app.automation.builder import STEP_KINDS, save_command
from app.tools.base import ToolError

MAX_ROWS = 10


class StepRow(QWidget):
    def __init__(self, on_remove, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = QComboBox()
        self.kind.addItem("Choose a step...", "")
        for kind in STEP_KINDS:
            self.kind.addItem(kind.label, kind.key)
        self.value = QLineEdit()
        self.kind.currentIndexChanged.connect(self._hint)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: on_remove(self))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.kind, 1)
        layout.addWidget(self.value, 2)
        layout.addWidget(remove)

    def _hint(self) -> None:
        key = self.kind.currentData()
        kind = next((k for k in STEP_KINDS if k.key == key), None)
        self.value.setPlaceholderText(kind.hint if kind else "")

    def values(self) -> tuple[str, str]:
        return self.kind.currentData() or "", self.value.text()


class CommandBuilderDialog(QDialog):
    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New voice command")
        self.setMinimumWidth(620)
        self.ctx = ctx
        self.saved = None
        self.name = QLineEdit()
        self.name.setPlaceholderText("What you'll say, e.g. gaming time")
        self.rows: list[StepRow] = []
        self.rows_box = QVBoxLayout()
        self.error = QLabel("")
        self.error.setObjectName("danger")
        self.error.setWordWrap(True)
        add = QPushButton("Add step")
        add.clicked.connect(self.add_row)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self.save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        form = QFormLayout()
        form.addRow("Say:", self.name)
        hint = QLabel("Steps run in order. Anything that changes or sends something still asks first, as usual.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addLayout(self.rows_box)
        layout.addWidget(self.error)
        layout.addLayout(buttons)
        self.add_row()

    def add_row(self) -> StepRow | None:
        if len(self.rows) >= MAX_ROWS:
            return None
        row = StepRow(self.remove_row, self)
        self.rows.append(row)
        self.rows_box.addWidget(row)
        return row

    def remove_row(self, row: StepRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
            row.setParent(None)
            row.deleteLater()

    def save(self) -> None:
        try:
            self.saved = save_command(self.ctx, self.name.text(), [row.values() for row in self.rows])
        except ToolError as exc:
            self.error.setText(str(exc))
            return
        self.accept()
