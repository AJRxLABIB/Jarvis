"""The contextual panel (right side of Chat): task progress, what the last answer used, the active devices and the
execution details. Controls (Cancel / Pause / Retry / Undo) appear only when the task supports them."""

from __future__ import annotations


from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar,
                               QPushButton, QTabWidget, QVBoxLayout, QWidget)

from app.ui.command_center import timeline
from app.ui.command_center.approval import ApprovalView


class TaskTab(QWidget):
    command = Signal(str)          # a checked request to send to Jarvis (permissions and approvals still apply)
    review_requested = Signal()
    mission_request = Signal(str, str)   # (mission id, "pause" | "cancel") for a RUNNING mission

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current: timeline.Timeline | None = None
        self.approval = QLabel()
        self.approval.setWordWrap(True)
        self.approval.setObjectName("card")
        self.approval.setAccessibleName("Waiting for your approval")
        self.approval.setTextFormat(Qt.TextFormat.PlainText)
        self.approval.setVisible(False)
        self.title = QLabel("No task is running.")
        self.title.setWordWrap(True)
        self.title.setObjectName("h1")
        self.title.setAccessibleName("Current task")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName("Task progress")
        self.bar.setVisible(False)
        self.steps = QListWidget()
        self.steps.setAccessibleName("Task timeline")
        self.steps.setWordWrap(True)
        self.steps.setAlternatingRowColors(False)
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        self.buttons: dict[str, QPushButton] = {}
        row = QHBoxLayout()
        for key, label, tip, slot in (
                ("pause", "Pause", "Stop before the next step; continue later", lambda: self._mission("pause")),
                ("cancel", "Cancel", "Cancel this task", self._cancel),
                ("retry", "Retry", "Continue from the step that stopped", lambda: self.command.emit(timeline.COMMANDS["retry"])),
                ("undo", "Undo", "Undo the finished steps that can be undone, newest first",
                 lambda: self.command.emit(timeline.COMMANDS["undo"])),
                ("review", "Review", "Open the full record of this task", self.review_requested.emit)):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setAccessibleName(f"{label} task")
            b.clicked.connect(lambda _=False, s=slot: s())
            self.buttons[key] = b
            row.addWidget(b)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.approval)
        layout.addWidget(self.title)
        layout.addWidget(self.bar)
        layout.addWidget(self.steps, 1)
        layout.addWidget(self.note)
        layout.addLayout(row)
        self.show_timeline(None)

    def _mission(self, kind: str) -> None:
        if self.current is not None:
            self.mission_request.emit(self.current.mission_id, kind)

    def _cancel(self) -> None:
        if self.current is None:
            return
        if self.current.state in ("running", "verifying"):
            self._mission("cancel")                    # honoured before the next step
        else:
            self.command.emit(timeline.COMMANDS["cancel"])

    def show_approval(self, view: ApprovalView | None) -> None:
        if view is None:
            self.approval.setVisible(False)
            return
        self.approval.setText("⏸ Waiting for your approval\n" + "\n".join(view.lines()))
        self.approval.setVisible(True)

    def show_timeline(self, tl: timeline.Timeline | None) -> None:
        self.current = tl
        self.steps.clear()
        if tl is None:
            self.title.setText("No task is running.")
            self.bar.setVisible(False)
            self.note.setText("Multi-step tasks (missions) appear here as a timeline.")
            for b in self.buttons.values():
                b.setVisible(False)
            return
        self.title.setText(f"{tl.title}  -  {tl.state_label}")
        done = sum(1 for r in tl.rows if r.state in ("done", "skipped"))
        self.bar.setVisible(True)
        self.bar.setRange(0, max(1, len(tl.rows)))
        self.bar.setValue(done)
        self.bar.setToolTip(f"{done} of {len(tl.rows)} steps done")
        for row in tl.rows:
            item = QListWidgetItem(row.text)
            item.setToolTip(row.detail or row.label)
            if row.n == tl.current:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.steps.addItem(item)
        if tl.current is not None:
            self.steps.setCurrentRow(max(0, tl.current - 1))
        self.note.setText(tl.note or "")
        c = tl.controls
        for key, shown in (("pause", c.pause), ("cancel", c.cancel), ("retry", c.retry), ("undo", c.undo),
                           ("review", c.review)):
            self.buttons[key].setVisible(bool(shown))


class TextTab(QWidget):
    def __init__(self, name: str, empty: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.empty = empty
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName(name)
        self.text.setMaximumBlockCount(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.text)
        self.set_lines([])

    def set_lines(self, lines: list[str]) -> None:
        self.text.setPlainText("\n".join(lines) if lines else self.empty)

    def append(self, line: str) -> None:
        if self.text.toPlainText() == self.empty:
            self.text.clear()
        self.text.appendPlainText(line)


class ContextPanel(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("contextPanel")
        self.setAccessibleName("Context panel")
        self.setMinimumWidth(300)
        self.task = TaskTab()
        self.sources = TextTab("What the last answer used", "Nothing yet. After an answer, the tools and memories it used are listed here.")
        self.device = TextTab("Active devices", "No device information yet.")
        self.details = TextTab("Execution details", "Each action Jarvis runs is listed here as it happens.")
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Context")
        for widget, name in ((self.task, "Task"), (self.sources, "Used"), (self.device, "Device"), (self.details, "Details")):
            self.tabs.addTab(widget, name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.tabs)

    def show_sources(self, sources: dict | None) -> None:
        lines: list[str] = []
        for t in (sources or {}).get("tools", [])[:12]:
            state = "worked" if t["ok"] else "FAILED"
            if t["ok"] and t.get("verified") is True:
                state += ", checked"
            elif t["ok"] and t.get("verified") is False:
                state = "not confirmed"
            lines.append(f"{'✔' if t['ok'] else '✖'} {t['tool']} ({state})")
        n = len((sources or {}).get("memories", []))
        if n:
            lines.append(f"● {n} saved memor{'y' if n == 1 else 'ies'} used")
        if lines:
            lines.append("Everything else is the AI's own wording (not checked).")
        self.sources.set_lines(lines)

    def show_devices(self, cards) -> None:
        lines: list[str] = []
        for c in cards:
            lines.append(c.headline)
            lines += [f"   {cap.glyph} {cap.name}" + (f" - {cap.why}" if cap.why else "") for cap in c.capabilities]
        self.device.set_lines(lines)
