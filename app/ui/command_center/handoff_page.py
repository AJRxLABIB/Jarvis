"""Handoff screen: move an unfinished mission to a paired device and see where every package stands. Rules and checks live in
app/core/handoff.py (signed, expiring, one device, the device checks its own permissions); this page only draws and calls the existing
``handoff`` tool as your own command. Not exercised by automated tests (needs Qt + a display)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QListWidget, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from app.ui.command_center import handoff_data as hd


class HandoffPage(QWidget):
    def __init__(self, load: Callable[[], list[hd.Row]], phones: Callable[[], list[tuple[str, str]]],
                 run_tool: Callable[[str, dict, Callable[[str], None]], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.load, self.phones, self.run_tool = load, phones, run_tool
        self.rows: list[hd.Row] = []
        title = QLabel("Handoff")
        title.setObjectName("h1")
        intro = QLabel("Move an unfinished task to your phone, review it there, and continue here after you approve. A handoff works once, "
                       "only for the device you pick, and expires. The phone checks its own permissions; nothing is inherited from this PC.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        self.device = QComboBox()
        self.device.setAccessibleName("Device to hand over to")
        self.create = QPushButton("Hand over the unfinished task")
        self.create.setToolTip("Package the unfinished mission for the chosen device (valid 30 minutes)")
        self.create.clicked.connect(self._create)
        self.revoke = QPushButton("Revoke selected")
        self.revoke.clicked.connect(self._revoke)
        self.revoke_all = QPushButton("Revoke all unused")
        self.revoke_all.clicked.connect(self._revoke_all)
        self.listing = QListWidget()
        self.listing.setAccessibleName("Handoffs")
        self.listing.currentRowChanged.connect(self._show)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setAccessibleName("Handoff details")
        self.message = QLabel("")
        self.message.setWordWrap(True)
        bar = QHBoxLayout()
        for w in (QLabel("To:"), self.device, self.create, self.revoke, self.revoke_all):
            bar.addWidget(w)
        bar.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addLayout(bar)
        layout.addWidget(self.listing, 1)
        layout.addWidget(self.detail, 1)
        layout.addWidget(self.message)

    def refresh(self) -> None:
        phones = self.phones()
        current = self.device.currentData()
        self.device.blockSignals(True)
        self.device.clear()
        for device_id, name in phones:
            self.device.addItem(name, device_id)
        if current is not None and self.device.findData(current) >= 0:
            self.device.setCurrentIndex(self.device.findData(current))
        self.device.blockSignals(False)
        self.create.setEnabled(bool(phones))
        if not phones:
            self.message.setText("No trusted phone is paired yet (Menu > Mobile devices). Handoff needs one.")
        try:
            self.rows = self.load()
        except Exception:
            self.rows = []
        selected = self.listing.currentRow()
        self.listing.blockSignals(True)
        self.listing.clear()
        for row in self.rows:
            self.listing.addItem(row.line())
        self.listing.blockSignals(False)
        if self.rows:
            self.listing.setCurrentRow(max(0, min(selected, len(self.rows) - 1)))
            self._show(self.listing.currentRow())
        else:
            self.detail.setPlainText("No handoffs yet.")
        self.revoke.setEnabled(any(r.state == "issued" for r in self.rows))
        self.revoke_all.setEnabled(any(r.state == "issued" for r in self.rows))

    def _show(self, i: int) -> None:
        if 0 <= i < len(self.rows):
            r = self.rows[i]
            self.detail.setPlainText(f"{r.line()}\n\n{r.note}")

    def _say(self, text: str) -> None:
        self.message.setText(text)

    def _create(self) -> None:
        device = self.device.currentData()
        if device:
            self.run_tool("handoff", {"action": "create", "device_id": device}, self._say)

    def _revoke(self) -> None:
        i = self.listing.currentRow()
        if 0 <= i < len(self.rows) and self.rows[i].state == "issued":
            self.run_tool("handoff", {"action": "revoke", "handoff_id": self.rows[i].id}, self._say)

    def _revoke_all(self) -> None:
        for r in self.rows:
            if r.state == "issued":
                self.run_tool("handoff", {"action": "revoke", "handoff_id": r.id}, self._say)
