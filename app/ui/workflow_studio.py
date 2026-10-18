"""Workflow studio: look at, check and safely edit your saved workflows and automations.

Left: everything saved. Right: when it runs, its steps (risk, condition, whether it asks first) and buttons to
reorder, remove, add a spoken note, set a condition, or toggle "continue on error". "Dry run" shows what a run would
do right now WITHOUT running anything. "Save" validates the steps exactly like creating the automation does, keeps
the old version (restore it from the Versions list) and never changes when it runs.
"Run now" / "Resume" are sent as normal requests, so permissions and confirmations apply as usual.

Keyboard: Alt+D dry run, Alt+S save, Alt+U/Alt+W move up/down, Alt+X remove step, Alt+N add note, Alt+O condition,
Alt+I simulate a what-if, Alt+E continue-on-error, Alt+R run now, Alt+M resume, Esc closes.
Ctrl+Z undoes the last unsaved change.

Two views of the same steps: the Steps table (best for screen readers) and the Canvas (boxes and arrows). On the canvas,
click selects, dragging a box to another slot reorders, arrows move the selection, Ctrl+Left/Right move the selected
step and Delete removes it - all through the same checked edits as the buttons.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from app.automation import studio
from app.automation.tasks import AutomationStore, AutomationTask, Step
from app.automation import canvas as geo
from app.tools.base import ToolError
from app.ui.workflow_canvas import WorkflowCanvas


class WorkflowStudioDialog(QDialog):
    def __init__(self, ctx, send_text=None, parent: QWidget | None = None, *, registry=None, permissions=None,
                 plugged=lambda: None) -> None:
        super().__init__(parent)
        self.ctx, self.send_text = ctx, send_text
        self.registry = registry if registry is not None else ctx.get_tool_registry()
        self.permissions = permissions if permissions is not None else ctx.executor.permissions
        self.plugged = plugged
        self.store = AutomationStore(ctx.database)
        self.tasks: list[AutomationTask] = []
        self.current: AutomationTask | None = None
        self.steps: list[Step] = []
        self.dirty = False
        self.undo_stack: list[list[Step]] = []      # earlier states of the steps being edited (not yet saved)
        self.setWindowTitle("Workflow studio")
        self.setMinimumSize(980, 560)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Status message")
        self.listing = QListWidget()
        self.listing.setAccessibleName("Saved workflows and automations")
        self.listing.currentRowChanged.connect(self._choose)
        self.when = QLabel()
        self.when.setWordWrap(True)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Step", "Risk", "Only if", "On error"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setAccessibleName("Steps")
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setAccessibleName("Dry run report")
        self.versions = QComboBox()
        self.versions.setAccessibleName("Earlier versions")

        def button(label: str, handler, tip: str = "") -> QPushButton:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(handler)
            return b

        self.buttons = {
            "dry": button("&Dry run", self.dry_run, "Show what would happen now, without running anything"),
            "sim": button("S&imulate...", self.simulate, "Play a what-if (a step fails, Jarvis crashes, ...) with pretend "
                                                         "tools. Nothing real runs."),
            "up": button("Move &up", lambda: self._edit("up")),
            "down": button("Move do&wn", lambda: self._edit("down")),
            "remove": button("Remo&ve step", lambda: self._edit("remove")),
            "note": button("Add &note", self._add_note, "Insert a spoken message after this step"),
            "cond": button("C&ondition...", self._condition, "Run this step only if..."),
            "err": button("Continue on &error", lambda: self._edit("continue_on_error")),
            "undo": button("Undo", self.undo, "Take back the last change (before saving)"),
            "save": button("&Save", self.save, "Check and store; the old version is kept"),
            "run": button("&Run now", lambda: self._send("run {name}")),
            "resume": button("Re&sume", lambda: self._send("resume {name}"), "Continue a workflow that stopped"),
            "restore": button("Restore ver&sion", self.restore_version),
        }
        side = QVBoxLayout()
        for key in ("dry", "sim", "up", "down", "remove", "note", "cond", "err", "undo", "save"):
            side.addWidget(self.buttons[key])
        side.addStretch(1)
        for key in ("run", "resume"):
            side.addWidget(self.buttons[key])
        restore = QHBoxLayout()
        restore.addWidget(self.versions, 1)
        restore.addWidget(self.buttons["restore"])
        right = QVBoxLayout()
        right.addWidget(self.when)
        self.canvas = WorkflowCanvas()
        self.canvas.selected.connect(self._canvas_selected)
        self.canvas.moved.connect(self._canvas_moved, Qt.ConnectionType.QueuedConnection)   # the dropped box is deleted on redraw
        self.canvas.nudged.connect(self._canvas_nudged)
        self.canvas.removed.connect(self._canvas_removed)
        self.views = QTabWidget()
        self.views.setAccessibleName("Steps view")
        self.views.addTab(self.table, "&Steps")
        self.views.addTab(self.canvas, "&Canvas")
        self.table.itemSelectionChanged.connect(self._table_selected)
        middle = QHBoxLayout()
        middle.addWidget(self.views, 1)
        middle.addLayout(side)
        right.addLayout(middle, 2)
        right.addWidget(self.report, 1)
        right.addLayout(restore)
        body = QHBoxLayout()
        body.addWidget(self.listing, 1)
        body.addLayout(right, 3)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(self.status)
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.undo)
        self.refresh()

    # ------------------------------------------------------------------ data

    def refresh(self, keep: str | None = None) -> None:
        self.tasks = self.store.all()
        self.listing.blockSignals(True)
        self.listing.clear()
        for task in self.tasks:
            kind = "workflow" if task.is_workflow else task.trigger.type
            self.listing.addItem(f"{task.name}  ({kind}{'' if task.enabled else ', paused'})")
        self.listing.blockSignals(False)
        index = next((i for i, t in enumerate(self.tasks) if t.name == keep), 0 if self.tasks else -1)
        self.listing.setCurrentRow(index)
        self._choose(index)
        if not self.tasks:
            self._say("Nothing is saved yet. Ask Jarvis to 'save a workflow ...' or 'every morning at 8 ...'.")

    def _choose(self, row: int) -> None:
        self.current = self.tasks[row] if 0 <= row < len(self.tasks) else None
        self.steps = [s.model_copy(deep=True) for s in self.current.steps] if self.current else []
        self.dirty = False
        self.undo_stack = []
        self.report.clear()
        self.when.setText(f"Runs: {self.current.trigger.describe()}. Last result: {self.current.last_status or 'never run'}."
                          if self.current else "")
        self.versions.clear()
        if self.current:
            for v in studio.previous_versions(self.store, self.current.name):
                self.versions.addItem(f"version {v['version']} - {v['saved_at'][:16]} ({v['reason']})", v)
        self._fill()

    def _fill(self) -> None:
        rows = self._reports()
        self._redraw_canvas(rows)
        self.table.setRowCount(len(self.steps))
        for i, step in enumerate(self.steps):
            r = rows[i] if i < len(rows) else None
            cells = (step.describe(), (r.risk + (", asks first" if r and r.asks else "")) if r else "",
                     step.when.replace("_", " ") if step.when else "", "continues" if step.continue_on_error else "stops")
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if r is not None and r.problem and col == 1:
                    item.setToolTip(r.problem)
                    item.setForeground(Qt.GlobalColor.red)
                self.table.setItem(i, col, item)
        enabled = self.current is not None
        for key in ("dry", "sim", "run", "restore"):
            self.buttons[key].setEnabled(enabled)
        has_steps = bool(self.steps)
        for key in ("up", "down", "remove", "note", "cond", "err"):
            self.buttons[key].setEnabled(has_steps)
        self.buttons["save"].setEnabled(self.dirty)
        self.buttons["undo"].setEnabled(bool(self.undo_stack))
        self.buttons["resume"].setEnabled(bool(self.current and self.current.is_workflow))
        self.buttons["restore"].setEnabled(self.versions.count() > 0)
        if has_steps and self.table.currentRow() < 0:
            self.table.selectRow(0)

    # ------------------------------------------------------------------ canvas

    def _redraw_canvas(self, rows: list) -> None:
        self.canvas.show_steps(self.steps, rows, self.table.currentRow())

    def _canvas_selected(self, index: int) -> None:
        if 0 <= index < self.table.rowCount():
            self.table.selectRow(index)

    def _table_selected(self) -> None:
        self.canvas.select(self.table.currentRow())

    def _canvas_moved(self, source: int, target: int) -> None:
        """A node was dropped on another slot: the same one-step edits as Move up / Move down, one after the other."""
        try:
            steps = self.steps
            for offset, action in enumerate(geo.moves(source, target)):
                steps = studio.edit_steps(steps, action, source + offset if action == "down" else source - offset)
        except ToolError as exc:
            self._say(str(exc))
            return
        self._remember()
        self.steps, self.dirty = steps, True
        self._fill()
        self.table.selectRow(target)
        self._say("Changed (not saved yet).")

    def _canvas_nudged(self, index: int, action: str) -> None:
        self.table.selectRow(index)
        self._edit(action)

    def _canvas_removed(self, index: int) -> None:
        self.table.selectRow(index)
        self._edit("remove")

    def _reports(self) -> list[studio.StepReport]:
        if self.current is None:
            return []
        draft = AutomationTask(self.current.id, self.current.name, self.current.enabled, self.current.trigger,
                               self.steps, None, None, None)
        return studio.dry_run(draft, self.registry, self.ctx, self.permissions,
                              weekday=datetime.now().weekday() < 5, plugged=self.plugged())

    def _say(self, text: str) -> None:
        self.status.setText(text)

    # ---------------------------------------------------------------- actions

    def _row(self) -> int:
        return max(self.table.currentRow(), 0)

    def _edit(self, action: str, value: str | None = None) -> None:
        row = self._row()
        try:
            changed = studio.edit_steps(self.steps, action, row, value)
        except ToolError as exc:
            self._say(str(exc))
            return
        self._remember()
        self.steps = changed
        self.dirty = True
        self._fill()
        target = {"up": row - 1, "down": row + 1, "add_note": row + 1}.get(action, row)
        self.table.selectRow(min(max(target, 0), max(len(self.steps) - 1, 0)))
        self._say("Changed (not saved yet).")

    def _remember(self) -> None:
        self.undo_stack.append([s.model_copy(deep=True) for s in self.steps])
        del self.undo_stack[:-50]

    def undo(self) -> None:
        if not self.undo_stack:
            return
        self.steps = self.undo_stack.pop()
        self.dirty = bool(self.undo_stack) or self.steps != (self.current.steps if self.current else [])
        self._fill()
        self._say("Undone." + ("" if self.dirty else " Back to the saved version."))

    def _add_note(self) -> None:
        text, ok = QInputDialog.getText(self, "Add note", "What should Jarvis say at this point?")
        if ok and text.strip():
            self._edit("add_note", text)

    def _condition(self) -> None:
        names = ["always", *[c.replace("_", " ") for c in studio.CONDITIONS if c]]
        choice, ok = QInputDialog.getItem(self, "Condition", "Run this step only if:", names, 0, False)
        if ok:
            self._edit("condition", "" if choice == "always" else choice.replace(" ", "_"))

    def dry_run(self) -> None:
        if self.current is None:
            return
        rows = self._reports()
        self.report.setPlainText("Dry run - nothing was run.\n" + studio.describe(rows))
        self._say(f"{len(rows)} step(s) checked; {sum(1 for r in rows if r.problem)} problem(s).")

    def simulate(self, what_if: str | None = None) -> None:
        """Play a what-if through the steps as they are now (saved or not). Pretend tools, scratch database, nothing real."""
        from app.automation import simulator as sim

        if self.current is None:
            return
        raw = [s.model_dump(exclude_none=True) for s in self.steps]
        tools = [s["tool"] for s in raw if s.get("type", "tool") == "tool"]
        if not tools:
            self._say("This workflow has no tool steps to simulate.")
            return
        if what_if is None:
            labels = [sim.WHAT_IF_LABELS[w] for w in sim.WHAT_IFS]
            choice, ok = QInputDialog.getItem(self, "Simulate", "What if...", labels, 0, False)
            if not ok:
                return
            what_if = sim.WHAT_IFS[labels.index(choice)]
        target = tools[0]
        row = self.table.currentRow()
        if 0 <= row < len(raw) and raw[row].get("type", "tool") == "tool":
            target = raw[row]["tool"]                      # the selected step is the one the what-if happens to
        try:
            report = sim.simulate_workflow(raw, sim.scenario_for(what_if, target), self.registry)
        except Exception as exc:                            # a simulation problem must never break the studio
            self._say(f"The simulation could not run: {exc}")
            return
        self.report.setPlainText(report.describe())
        verdict = "found a step that would run twice" if report.duplicated else "no duplicated effects"
        self._say(f"Simulated '{sim.WHAT_IF_LABELS[what_if]}' on '{target}': {verdict}. Nothing real was run.")

    def save(self) -> None:
        if self.current is None or not self.dirty:
            return
        try:
            saved = studio.save(self.store, self.current, self.steps, self.registry, self.ctx)
        except ToolError as exc:
            self._say(f"Not saved: {exc}")
            return
        self._say(f"Saved '{saved.name}'. The previous version is kept under Versions.")
        self.refresh(keep=saved.name)

    def restore_version(self) -> None:
        data = self.versions.currentData()
        if self.current is None or data is None:
            return
        if QMessageBox.question(self, "Restore", f"Restore version {data['version']} of '{self.current.name}'? "
                                "The current one is kept as a version too.") != QMessageBox.StandardButton.Yes:
            return
        try:
            studio.save(self.store, self.current, data["steps"], self.registry, self.ctx)
        except ToolError as exc:
            self._say(f"Not restored: {exc}")
            return
        self._say(f"Restored version {data['version']}.")
        self.refresh(keep=self.current.name)

    def _send(self, template: str) -> None:
        if self.current is None or self.send_text is None:
            return
        if self.dirty:
            self._say("Save your changes first.")
            return
        self.send_text(template.format(name=self.current.name))
        self._say("Sent as a request; Jarvis will ask if anything needs your approval.")
