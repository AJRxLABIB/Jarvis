"""The Dashboard: cards built from real state (app/ui/command_center/dashboard_data.py). Only draws; the words are decided there.
Not exercised by automated tests (needs Qt + a display)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.ui.command_center import dashboard_data as data


class CardWidget(QFrame):
    def __init__(self, card: data.Card) -> None:
        super().__init__()
        self.setObjectName("card")
        self.setProperty("tone", card.tone)
        title = QLabel(card.title)
        title.setObjectName("muted")
        head = QLabel(card.headline)
        head.setObjectName("h1")
        head.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        layout.addWidget(title)
        layout.addWidget(head)
        for line in card.lines:
            label = QLabel(line)
            label.setWordWrap(True)
            layout.addWidget(label)
        layout.addStretch(1)
        self.setAccessibleName(f"{card.title}: {card.headline}")
        self.setAccessibleDescription(" ".join(card.lines))


class DashboardPage(QWidget):
    def __init__(self, build: Callable[[], list[data.Card]], actions: list[tuple[str, str, Callable[[], None] | None]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.build = build
        self._signature: tuple = ()
        title = QLabel("Dashboard")
        title.setObjectName("h1")
        self.grid = QGridLayout()
        self.grid.setSpacing(10)
        row = QHBoxLayout()
        for label, tip, slot in actions:
            button = QPushButton(label)
            button.setToolTip(tip)
            button.setEnabled(slot is not None)
            if slot is not None:
                button.clicked.connect(lambda _=False, s=slot: s())
            row.addWidget(button)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(self.grid, 1)
        layout.addLayout(row)

    def refresh(self) -> None:
        try:
            cards = self.build()
        except Exception:
            cards = [data.Card("error", "Dashboard", "Not available right now.", [], "warn")]
        signature = tuple((c.key, c.headline, tuple(c.lines), c.tone) for c in cards)
        if signature == self._signature:               # nothing changed: do not rebuild (no flicker, no needless work)
            return
        self._signature = signature
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for i, card in enumerate(cards):
            self.grid.addWidget(CardWidget(card), i // 3, i % 3)
