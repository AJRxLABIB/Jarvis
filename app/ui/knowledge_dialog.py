"""Knowledge: ask your own knowledge graph (projects, files, decisions, memories, goals, requests), see the evidence."""

from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from app.core import knowledge_graph

EXAMPLES = ("what was I working on last week", "what decision did I make about the database",
            "which files are related to my project", "which project uses laravel", "everything related to TMS")


class KnowledgeDialog(QDialog):
    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Knowledge")
        self.setMinimumSize(820, 560)
        self.ctx = ctx
        counts = knowledge_graph.summary(ctx)
        overview = QLabel("What I know, linked: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) +
                          ". Built from your projects, git, memories, goals, missions and your requests - nothing "
                          "extra is collected.")
        overview.setWordWrap(True)
        self.question = QLineEdit()
        self.question.setPlaceholderText("Ask, e.g. " + EXAMPLES[0])
        self.question.returnPressed.connect(self.ask)
        ask = QPushButton("Ask")
        ask.clicked.connect(self.ask)
        row = QHBoxLayout()
        row.addWidget(self.question, 1)
        row.addWidget(ask)
        self.answer = QLabel()
        self.answer.setWordWrap(True)
        self.evidence = QListWidget()
        examples = QListWidget()
        examples.addItems(EXAMPLES)
        examples.itemClicked.connect(lambda item: (self.question.setText(item.text()), self.ask()))
        examples.setMaximumHeight(110)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        ask_page = QWidget()
        layout = QVBoxLayout(ask_page)
        layout.addWidget(overview)
        layout.addLayout(row)
        layout.addWidget(examples)
        layout.addWidget(self.answer)
        layout.addWidget(QLabel("Evidence"))
        layout.addWidget(self.evidence, 1)
        self.tabs = QTabWidget()
        self.tabs.addTab(ask_page, "Ask")
        self._browser = None
        self.tabs.addTab(QWidget(), "Browse")
        self.tabs.currentChanged.connect(self._open_browser)
        outer = QVBoxLayout(self)
        outer.addWidget(self.tabs, 1)
        outer.addWidget(close)

    def _open_browser(self, index: int) -> None:
        """The map is built the first time Browse is opened (reading projects and files takes a moment)."""
        if index != 1 or self._browser is not None:
            return
        from app.ui.graph_browser import GraphBrowser

        self._browser = GraphBrowser(self.ctx)
        self.tabs.blockSignals(True)
        self.tabs.removeTab(1)
        self.tabs.insertTab(1, self._browser, "Browse")
        self.tabs.setCurrentIndex(1)
        self.tabs.blockSignals(False)

    def ask(self) -> None:
        text = self.question.text().strip()
        if not text:
            return
        answer, evidence, _kind = knowledge_graph.ask(self.ctx, text)
        self.answer.setText(answer)
        self.evidence.clear()
        self.evidence.addItems(evidence)
