"""Memory manager: view, search, add, delete and clear saved facts."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.memory.manager import MemoryManager
from app.tools.base import ToolError


class MemoryDialog(QDialog):
    def __init__(self, manager: MemoryManager, parent: QWidget | None = None, ask=None, suggestions=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Memory")
        self.setMinimumSize(640, 420)
        self.manager = manager
        self.suggestions = suggestions  # app.memory.suggestions.SuggestionStore
        self._ask = ask or (lambda title, text: QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes)
        self._show = (lambda text: QMessageBox.information(self, "Why I remember this", text)) if ask is None else None

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search memories...")
        self.search.textChanged.connect(self.refresh)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["#", "Fact", "Saved", "Source", "Status", "Type", "Confidence"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.count = QLabel()
        self.count.setObjectName("muted")

        add = QPushButton("Add...")
        add.clicked.connect(self.add)
        why = QPushButton("Why?")
        why.setToolTip("Why Jarvis remembers it and where it came from")
        why.clicked.connect(self.explain_selected)
        self.toggle_button = QPushButton("Use / don't use")
        self.toggle_button.clicked.connect(self.toggle_selected)
        self.delete_button = QPushButton("Delete selected")
        self.delete_button.clicked.connect(self.delete_selected)
        clear = QPushButton("Clear all")
        clear.setObjectName("danger")
        clear.clicked.connect(self.clear_all)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(why)
        buttons.addWidget(self.toggle_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(clear)
        buttons.addStretch()
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Only facts you asked the assistant to remember are stored, on this computer. "
                                "\"Why?\" shows where each one came from; switched-off, private and expiring ones are marked."))
        layout.addWidget(self.search)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.count)
        self.suggestion_list = QListWidget()
        self.suggestion_list.setMaximumHeight(120)
        self.suggestion_label = QLabel("SUGGESTIONS  ·  noticed in our conversations, saved only if you accept")
        self.suggestion_label.setObjectName("sectionLabel")
        accept = QPushButton("Accept")
        accept.setObjectName("primary")
        accept.clicked.connect(self.accept_suggestion)
        reject = QPushButton("Reject")
        reject.clicked.connect(self.reject_suggestion)
        suggestion_buttons = QHBoxLayout()
        suggestion_buttons.addWidget(accept)
        suggestion_buttons.addWidget(reject)
        suggestion_buttons.addStretch()
        self.suggestion_box = QWidget()
        box = QVBoxLayout(self.suggestion_box)
        box.setContentsMargins(0, 6, 0, 0)
        box.addWidget(self.suggestion_label)
        box.addWidget(self.suggestion_list)
        box.addLayout(suggestion_buttons)
        layout.addWidget(self.suggestion_box)
        layout.addLayout(buttons)
        self.refresh()
        self.refresh_suggestions()

    def refresh(self) -> None:
        query = self.search.text().strip()
        memories = (self.manager.search(query, limit=500, include_hidden=True) if query
                    else self.manager.all(500, include_hidden=True))
        self.table.setRowCount(len(memories))
        for row, memory in enumerate(memories):
            id_item = QTableWidgetItem(str(memory.id))
            id_item.setData(Qt.ItemDataRole.UserRole, memory.id)
            self.table.setItem(row, 0, id_item)
            self.table.setItem(row, 1, QTableWidgetItem(memory.content))
            self.table.setItem(row, 2, QTableWidgetItem(memory.updated_at[:16].replace("T", " ")))
            from app.memory.manager import PROVENANCE

            self.table.setItem(row, 3, QTableWidgetItem(PROVENANCE.get(memory.provenance, memory.provenance)
                                                        .replace("you ", "", 1)))
            status = [s for s in ("off" if memory.disabled else "", "private" if memory.sensitivity == "private"
                                  else "", f"until {memory.expires_at[:10]}" if memory.expires_at else "",
                                  f"v{memory.revision}" if memory.revision > 1 else "") if s]
            if memory.superseded_by:
                status.insert(0, f"replaced by #{memory.superseded_by}")
            self.table.setItem(row, 4, QTableWidgetItem(", ".join(status) or "in use"))
            self.table.setItem(row, 5, QTableWidgetItem(memory.kind))
            self.table.setItem(row, 6, QTableWidgetItem(f"{memory.confidence:.0%}" + ("" if memory.stated
                                                                                      else " (recorded)")))
        self.count.setText(f"{len(memories)} shown")

    def refresh_suggestions(self) -> None:
        self.suggestion_list.clear()
        items = self.suggestions.pending() if self.suggestions is not None else []
        for suggestion_id, text in items:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, suggestion_id)
            self.suggestion_list.addItem(item)
        self.suggestion_box.setVisible(bool(items))

    def _selected_suggestion(self) -> int | None:
        item = self.suggestion_list.currentItem() or self.suggestion_list.item(0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def accept_suggestion(self) -> None:
        suggestion_id = self._selected_suggestion()
        if suggestion_id is None:
            return
        try:
            self.suggestions.accept(suggestion_id, self.manager)
        except ToolError as exc:
            QMessageBox.warning(self, "Not saved", str(exc))
        self.refresh()
        self.refresh_suggestions()

    def reject_suggestion(self) -> None:
        suggestion_id = self._selected_suggestion()
        if suggestion_id is None:
            return
        self.suggestions.reject(suggestion_id)
        self.refresh_suggestions()

    def selected_ids(self) -> list[int]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in sorted(rows)]

    def explain_selected(self) -> str:
        ids = self.selected_ids()
        if not ids:
            return ""
        memory = self.manager.get(ids[0])
        text = self.manager.explain(memory) if memory is not None else ""
        if text and self._show is not None:
            self._show(text)
        return text

    _show = None

    def toggle_selected(self) -> None:
        for memory_id in self.selected_ids():
            memory = self.manager.get(memory_id)
            if memory is not None:
                self.manager.set_disabled(memory, not memory.disabled)
        self.refresh()

    def add(self, text: str | None = None) -> None:
        if text is None:
            text, ok = QInputDialog.getText(self, "Remember", "What should I remember?")
            if not ok:
                return
        try:
            self.manager.remember(text, provenance="typed")
        except ToolError as exc:
            QMessageBox.warning(self, "Not saved", str(exc))
            return
        self.refresh()

    def delete_selected(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        if not self._ask("Delete", f"Delete {len(ids)} memory(ies)?"):
            return
        for memory_id in ids:
            try:
                self.manager.forget(memory_id)
            except ToolError:
                pass
        self.refresh()

    def clear_all(self) -> None:
        total = len(self.manager.all(100_000))
        if total and self._ask("Clear all", f"Delete all {total} memories? This can't be undone."):
            self.manager.clear()
            self.refresh()
