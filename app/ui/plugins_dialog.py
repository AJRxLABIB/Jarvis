"""Plugins: which plugin files there are, whether you trusted them, and trusting one (window only)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, \
    QVBoxLayout, QWidget

from app.plugins import plugin_status, trust_plugin

LABELS = {"trusted": "trusted", "changed": "CHANGED since you trusted it - not loaded", "untrusted": "not trusted"}


class PluginsDialog(QDialog):
    def __init__(self, folder: Path, enabled: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Plugins")
        self.setMinimumSize(560, 360)
        self.folder = Path(folder)
        info = QLabel(f"Plugin files in {self.folder}. A plugin runs with your rights: trust only code you wrote or "
                      "checked. Changed files are never run until you trust them again. "
                      + ("" if enabled else "Plugins are switched off (Settings -> Extensions)."))
        info.setWordWrap(True)
        self.list = QListWidget()
        trust = QPushButton("Trust selected")
        trust.clicked.connect(self.trust_selected)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(trust)
        buttons.addStretch()
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for plugin in plugin_status(self.folder):
            tools = ", ".join(plugin["tools"]) or "-"
            item = QListWidgetItem(f"{plugin['name']} {plugin['version']}  [{LABELS[plugin['status']]}]  tools: {tools}")
            item.setData(256, plugin["name"])
            self.list.addItem(item)
        if not self.list.count():
            self.list.addItem("No plugin files. Put a .py file with TOOLS in the folder above.")

    def trust_selected(self) -> None:
        item = self.list.currentItem()
        name = item.data(256) if item is not None else None
        if not name:
            return
        answer = QMessageBox.question(self, "Trust plugin", f"Trust {name}.py? Its code runs now (to list its tools) "
                                      "and every time Jarvis starts, with your rights.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            manifest = trust_plugin(self.folder, name)
        except Exception as exc:
            QMessageBox.warning(self, "Plugin", f"{name} didn't load, so it wasn't trusted: {exc}")
            return
        QMessageBox.information(self, "Plugin", f"Trusted {name} ({', '.join(manifest['tools']) or 'no tools'}). "
                                "Restart Jarvis to load it.")
        self.refresh()
