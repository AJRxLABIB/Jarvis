"""Privacy dashboard: what Jarvis keeps about you on this PC, with View and Delete for each kind."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from app import privacy
from app.utils.logging_setup import get_logger, log_event

if TYPE_CHECKING:  # pragma: no cover
    from app.main import AppContext

_log = get_logger("privacy")


class PrivacyDialog(QDialog):
    def __init__(self, ctx: "AppContext", *, on_changed: Callable[[str], None] | None = None,
                 parent: QWidget | None = None, confirm: Callable[[str], bool] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Privacy dashboard")
        self.setMinimumSize(760, 560)
        self.setStyleSheet(
            "QScrollArea#items, QWidget#itemsBody { background: #050c16; }"
            "QScrollArea#items { border: 1px solid #153a57; border-radius: 4px; }"
            "QPushButton#danger:disabled { background: #140a0a; border-color: #3a1c1c; color: #5a3a3a; }")
        self.ctx = ctx
        self.on_changed = on_changed
        self.confirm = confirm or self._ask
        self.summaries: dict[str, QLabel] = {}
        self.delete_buttons: dict[str, QPushButton] = {}

        title = QLabel("PRIVACY  ·  WHAT I KEEP ABOUT YOU")
        title.setObjectName("sectionLabel")
        intro = QLabel("Everything below is stored only on this PC, in your data folder. Nothing is uploaded. "
                       "Passwords, keys and tokens are never saved in memory or logs.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)

        grid_host = QWidget()
        grid_host.setObjectName("itemsBody")
        self.grid = QGridLayout(grid_host)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(10)
        scroll = QScrollArea()
        scroll.setObjectName("items")
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_host)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(110)
        self.preview.setPlaceholderText("Press View to see what is stored.")

        renew = QPushButton("Renew phone-remote key")
        renew.setToolTip("Paired phones stop working until they open the new address")
        renew.clicked.connect(self.renew_remote_key)
        open_folder = QPushButton("Open data folder")
        open_folder.clicked.connect(self.open_data_folder)
        tools = QHBoxLayout()
        tools.addWidget(renew)
        tools.addWidget(open_folder)
        tools.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.preview)
        layout.addLayout(tools)
        layout.addWidget(buttons)
        self.refresh()

    # ----------------------------------------------------------------- table

    def refresh(self) -> None:
        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self.summaries.clear()
        self.delete_buttons.clear()
        for row, item in enumerate(privacy.inventory(self.ctx)):
            name = QLabel(f"<b style='color:#e8f6ff'>{item.title}</b><br>"
                          f"<span style='color:#5f7f9c'>{item.description}</span>")
            name.setWordWrap(True)
            summary = QLabel(item.summary)
            summary.setObjectName("hudValue")
            view = QPushButton("View")
            view.clicked.connect(lambda _=False, k=item.key: self.show_preview(k))
            delete = QPushButton("Delete")
            delete.setObjectName("danger")
            delete.setEnabled(item.count > 0)
            delete.clicked.connect(lambda _=False, i=item: self.delete(i))
            self.grid.addWidget(name, row, 0)
            self.grid.addWidget(summary, row, 1)
            self.grid.addWidget(view, row, 2)
            self.grid.addWidget(delete, row, 3)
            self.grid.setColumnStretch(0, 1)
            self.summaries[item.key] = summary
            self.delete_buttons[item.key] = delete

    def show_preview(self, key: str) -> None:
        try:
            self.preview.setPlainText(privacy.preview(self.ctx, key))
        except Exception as exc:  # shown to the user
            self.preview.setPlainText(f"Could not read it: {exc}")

    def delete(self, item: privacy.DataItem) -> None:
        if not self.confirm(f"Delete {item.title.lower()} ({item.summary})? This cannot be undone."):
            return
        try:
            message = privacy.erase(self.ctx, item.key)
        except Exception as exc:  # shown to the user
            QMessageBox.warning(self, "Not deleted", str(exc))
            return
        log_event(_log, "privacy_erased", kind=item.key)
        if self.on_changed is not None:
            self.on_changed(item.key)
        self.preview.setPlainText(message)
        self.refresh()

    def renew_remote_key(self) -> None:
        if not self.confirm("Make a new phone-remote key? Paired phones must open the new address "
                            "(Menu -> Phone remote)."):
            return
        privacy.renew_remote_key(self.ctx)
        log_event(_log, "privacy_remote_key_renewed")
        if self.on_changed is not None:
            self.on_changed("remote_key")
        self.preview.setPlainText("New phone-remote key made. Old links no longer work.")

    def open_data_folder(self) -> None:
        from app.tools import winapi

        try:
            winapi.start_file(str(self.ctx.paths.data_dir))
        except Exception as exc:  # shown to the user
            self.preview.setPlainText(f"Could not open the folder: {exc}")

    def _ask(self, text: str) -> bool:
        answer = QMessageBox.question(self, "Privacy", text)
        return answer == QMessageBox.StandardButton.Yes
