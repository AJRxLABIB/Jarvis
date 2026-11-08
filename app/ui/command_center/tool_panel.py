"""A command-center page driven by a ``panel_specs.Panel``: read-only text blocks filled by existing tools, plus buttons that run existing tools as your own
command. Drawing only; the content and the rules live in the tools and in ``panel_specs`` (tested). Not exercised by automated tests (needs Qt + a display)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget

from app.ui.command_center import panel_specs as ps


class ToolPanelPage(QWidget):
    answered = Signal(str, str)          # block key / "" for the action line, text (from a worker thread, delivered on the UI thread)

    def __init__(self, panel: ps.Panel, run_tool: Callable[[str, dict, Callable[[str], None]], None],
                 extras: dict[str, Callable[[], None]] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.panel, self.run_tool = panel, run_tool
        self.boxes: dict[str, QPlainTextEdit] = {}
        self.answered.connect(self._show)
        title = QLabel(panel.title)
        title.setObjectName("h1")
        intro = QLabel(panel.intro)
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setAccessibleName(f"{panel.title} result")
        bar = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Read everything on this page again")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(refresh)
        for action in panel.actions:
            button = QPushButton(action.label)
            button.setToolTip(action.tip or action.label)
            button.clicked.connect(lambda _=False, a=action: self._act(a))
            bar.addWidget(button)
        for name in panel.extras:
            opener = (extras or {}).get(name)
            if opener is not None:
                button = QPushButton(name.removeprefix("open_").replace("_", " ").capitalize() + "...")
                button.clicked.connect(lambda _=False, fn=opener: fn())
                bar.addWidget(button)
        bar.addStretch(1)
        inner = QWidget()
        column = QVBoxLayout(inner)
        for i, block in enumerate(panel.blocks):
            box = QGroupBox(block.label)
            edit = QPlainTextEdit()
            edit.setReadOnly(True)
            edit.setMinimumHeight(90)
            edit.setAccessibleName(block.label)
            inside = QVBoxLayout(box)
            inside.addWidget(edit)
            column.addWidget(box)
            self.boxes[str(i)] = edit
        column.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addLayout(bar)
        layout.addWidget(self.message)
        layout.addWidget(scroll, 1)

    def refresh(self) -> None:
        for i, block in enumerate(self.panel.blocks):
            self.boxes[str(i)].setPlainText("Reading...")
            self.run_tool(block.tool, dict(block.args), lambda text, key=str(i): self.answered.emit(key, text))

    def _act(self, action: ps.Action) -> None:
        self.message.setText("Working...")
        self.run_tool(action.tool, dict(action.args), lambda text: self.answered.emit("", text))

    def _show(self, key: str, text: str) -> None:
        if key:
            self.boxes[key].setPlainText(text)
        else:
            self.message.setText(text)
            self.refresh()
