"""Workspaces: every Jarvis screen, grouped by what you're doing, in one place (MENU > Workspaces, Ctrl+K, or say
"show my workspaces"). Type to filter; Enter or a click opens it. Nothing is removed from the MENU - this is a
faster way in, not a replacement."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QGridLayout, QGroupBox, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

# workspace -> [(title, what it's for, MainWindow method)]
WORKSPACES: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("Today", (
        ("My day", "to-dos, plan, history", "open_my_day"),
        ("Life log", "journal, habits, expenses, schedule", "open_life_log"),
        ("Notifications", "what Jarvis told you", "open_notifications"),
        ("Background tasks", "what's running now", "open_task_center"),
    )),
    ("Control", (
        ("Mission control", "what Jarvis did, step by step", "open_mission_control"),
        ("Device manager", "computers, phones, smart devices", "open_device_manager"),
        ("Phone screen", "see and tap your phone", "open_phone_screen"),
        ("Mobile devices", "pair and trust phones", "open_mobile_devices"),
        ("Windows overview", "every open window", "open_overview"),
    )),
    ("Intelligence", (
        ("AI and models", "which model does which job", "open_models"),
        ("Knowledge", "your documents and notes", "open_knowledge"),
        ("Memory", "what Jarvis remembers", "open_memory"),
        ("Evolution center", "improvements Jarvis proposes", "open_evolution_center"),
        ("What Jarvis sees", "the context it uses", "open_context_panel"),
    )),
    ("Build", (
        ("Workflow studio", "multi-step workflows", "open_workflow_studio"),
        ("Automations", "schedules and triggers", "open_automations"),
        ("Plugins", "add-ons", "open_plugins"),
    )),
    ("Safety and system", (
        ("Security center", "permissions, approvals, audit", "open_security_center"),
        ("Privacy dashboard", "what's collected and kept", "open_privacy"),
        ("Emergency controls", "stop everything", "open_emergency"),
        ("Diagnostics", "health and recovery", "open_diagnostics"),
        ("Settings", "everything configurable", "open_settings"),
        ("Logs", "the technical record", "open_logs"),
    )),
)


def entries(window) -> list[tuple[str, str, str, str]]:
    """(workspace, title, note, method) for every screen this window really has."""
    return [(group, title, note, method) for group, items in WORKSPACES for title, note, method in items
            if callable(getattr(window, method, None))]


def matches(query: str, title: str, note: str, group: str) -> bool:
    words = query.lower().split()
    text = f"{title} {note} {group}".lower()
    return all(w in text for w in words)


class WorkspacesDialog(QDialog):
    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Workspaces")
        self.setMinimumSize(760, 520)
        self.window_ = window
        self.search = QLineEdit()
        self.search.setPlaceholderText("Type to find a screen (Enter opens the first)")
        self.search.setAccessibleName("Find a screen")
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self.open_first)
        self.buttons: list[tuple[QPushButton, tuple[str, str, str, str]]] = []
        body = QWidget()
        grid = QVBoxLayout(body)
        self.groups: dict[str, QGroupBox] = {}
        for group, _items in WORKSPACES:
            box = QGroupBox(group)
            layout = QGridLayout(box)
            col = row = 0
            for entry in entries(window):
                if entry[0] != group:
                    continue
                _, title, note, method = entry
                b = QPushButton(f"{title}\n{note}")
                b.setMinimumHeight(54)
                b.setAccessibleName(title)
                b.clicked.connect(lambda _=False, m=method: self.open(m))
                layout.addWidget(b, row, col)
                self.buttons.append((b, entry))
                col += 1
                if col == 3:
                    col, row = 0, row + 1
            self.groups[group] = box
            grid.addWidget(box)
        grid.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        hint = QLabel("Everything is also in the MENU. Ctrl+K opens this.")
        hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(scroll, 1)
        layout.addWidget(hint)

    def visible(self) -> list[str]:
        return [entry[3] for b, entry in self.buttons if not b.isHidden()]

    def _filter(self, text: str) -> None:
        for b, (group, title, note, _m) in self.buttons:
            b.setHidden(not matches(text, title, note, group))
        for group, box in self.groups.items():
            box.setHidden(all(b.isHidden() for b, e in self.buttons if e[0] == group))

    def open(self, method: str) -> None:
        self.accept()
        handler = getattr(self.window_, method, None)
        if callable(handler):
            handler()

    def open_first(self) -> None:
        shown = self.visible()
        if shown:
            self.open(shown[0])
