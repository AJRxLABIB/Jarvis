"""The notification center window and the emergency controls window."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
    QWidget,
)

from app.core import emergency
from app.core.notifications import CATEGORIES, NotificationCenter

ICONS = {"urgent": "!!", "high": "!", "normal": "·", "low": " "}


def _when(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        return ts[:16]


class NotificationDialog(QDialog):
    def __init__(self, center: NotificationCenter, run_action=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Notifications")
        self.setMinimumSize(620, 440)
        self.center, self.run_action = center, run_action
        self.filter = QComboBox()
        self.filter.addItem("All", "")
        for category in CATEGORIES:
            self.filter.addItem(category.capitalize(), category)
        self.filter.currentIndexChanged.connect(self.refresh)
        self.unread = QCheckBox("Unread only")
        self.unread.toggled.connect(self.refresh)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self.do_action())
        self.count = QLabel()
        self.count.setObjectName("muted")
        action = QPushButton("Do it")
        action.setToolTip("Run the suggested action (a command Jarvis wrote itself)")
        action.clicked.connect(self.do_action)
        read = QPushButton("Mark all read")
        read.clicked.connect(lambda: (self.center.mark_read(), self.refresh()))
        archive = QPushButton("Archive selected")
        archive.clicked.connect(self.archive_selected)
        clear = QPushButton("Clear all")
        clear.clicked.connect(lambda: (self.center.archive(), self.refresh()))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        top = QHBoxLayout()
        top.addWidget(QLabel("Show:"))
        top.addWidget(self.filter)
        top.addWidget(self.unread)
        top.addStretch()
        top.addWidget(self.count)
        buttons = QHBoxLayout()
        for button in (action, read, archive, clear):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        items = self.center.items(unread_only=self.unread.isChecked(), category=self.filter.currentData() or None)
        for notice in items:
            text = f"{ICONS[notice.priority]} {_when(notice.ts)}  {notice.title}" + (f" - {notice.body}" if notice.body else "")
            if notice.action:
                text += f"   [{notice.action}]"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, notice.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, notice.action)
            if not notice.read:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.list.addItem(item)
        self.count.setText(f"{self.center.unread_count()} unread")

    def selected(self) -> list[QListWidgetItem]:
        return self.list.selectedItems() or ([self.list.currentItem()] if self.list.currentItem() else [])

    def archive_selected(self) -> None:
        self.center.archive([i.data(Qt.ItemDataRole.UserRole) for i in self.selected()])
        self.refresh()

    def do_action(self) -> str | None:
        items = self.selected()
        if not items:
            return None
        action = items[0].data(Qt.ItemDataRole.UserRole + 1)
        self.center.mark_read([items[0].data(Qt.ItemDataRole.UserRole)])
        if action and self.run_action is not None:
            self.run_action(action)
            self.accept()
        else:
            self.refresh()
        return action or None


class EmergencyDialog(QDialog):
    """Only here (a person at the PC) can switches be turned back ON."""

    def __init__(self, on_stop, on_switch, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Emergency controls")
        self.setMinimumWidth(460)
        self.on_switch = on_switch
        stop = QPushButton("STOP EVERYTHING NOW")
        stop.setObjectName("danger")
        stop.clicked.connect(lambda: (on_stop(), self.refresh()))
        self.boxes: dict[str, QCheckBox] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(stop)
        hint = QLabel("Stops the current request and a running mission, clears the song queue, cancels a restart or "
                      "shutdown and switches automations off. Shortcut anywhere: Ctrl+Shift+F12.\n\nThese switches can "
                      "be turned on by voice, but turned off only here:")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        for switch in emergency.SWITCHES:
            box = QCheckBox(emergency.LABELS[switch].capitalize())
            box.toggled.connect(lambda on, s=switch: self.on_switch(s, on))
            self.boxes[switch] = box
            layout.addWidget(box)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
        self.refresh()

    def refresh(self) -> None:
        for switch, box in self.boxes.items():
            box.blockSignals(True)
            box.setChecked(emergency.STATE.flags.get(switch, False))
            box.blockSignals(False)
