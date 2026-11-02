"""The section pages of the command center: Devices, Security, Automations, Tasks, Dashboard and the launcher pages
(Memory, Knowledge, Developer Tools, Settings). Each page reads real data through the view-models beside it and
offers the existing full screens for anything that edits."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem,
                               QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from app.ui.command_center import automations as auto
from app.ui.command_center import devices as dev
from app.ui.command_center import graph_view as gv
from app.ui.command_center import security as sec
from app.ui.command_center import timeline


def _text(name: str, read_only: bool = True) -> QPlainTextEdit:
    box = QPlainTextEdit()
    box.setReadOnly(read_only)
    box.setAccessibleName(name)
    return box


def _button(label: str, tip: str, slot: Callable[[], None] | None, name: str = "") -> QPushButton:
    b = QPushButton(label)
    b.setToolTip(tip)
    b.setAccessibleName(name or label.strip("&.… "))
    b.setMinimumHeight(28)
    b.setEnabled(slot is not None)
    if slot is not None:
        b.clicked.connect(lambda _=False, s=slot: s())
    return b


def _heading(text: str) -> QLabel:
    h = QLabel(text)
    h.setObjectName("h1")
    return h


# ------------------------------------------------------------------ devices


class DevicesPage(QWidget):
    def __init__(self, collect: Callable[[], list[dev.DeviceCard]], *, open_phones=None, open_manager=None,
                 open_phone_screen=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.collect, self.cards = collect, []
        self.listing = QListWidget()
        self.listing.setAccessibleName("Devices")
        self.listing.currentRowChanged.connect(self._show)
        self.detail = _text("Device details")
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        self.refresh_button = _button("Refresh", "Read every device's state again", self.refresh)
        row = QHBoxLayout()
        row.addWidget(self.refresh_button)
        row.addWidget(_button("Phones...", "Pair, trust, restrict or revoke a phone", open_phones))
        row.addWidget(_button("Phone screen...", "See and tap your phone's screen", open_phone_screen))
        row.addWidget(_button("Device manager...", "Computers and network devices: add, check, remove", open_manager))
        row.addStretch(1)
        left = QVBoxLayout()
        left.addWidget(self.listing, 1)
        split = QHBoxLayout()
        split.addLayout(left, 2)
        split.addWidget(self.detail, 3)
        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Devices"))
        layout.addWidget(self.summary)
        layout.addLayout(split, 1)
        layout.addLayout(row)

    def refresh(self) -> None:
        keep = self.listing.currentRow()
        try:
            self.cards = self.collect()
        except Exception:
            self.cards = []
        self.listing.clear()
        for c in self.cards:
            self.listing.addItem(c.headline)
        phones = [c for c in self.cards if c.kind == "phone"]
        connected = sum(1 for c in self.cards if c.connection in ("connected", "local"))
        self.summary.setText(f"{connected} of {len(self.cards)} device(s) connected. "
                             + ("No phone is paired (MENU > Mobile devices > Pair a new phone)." if not phones else ""))
        if self.cards:
            self.listing.setCurrentRow(keep if 0 <= keep < len(self.cards) else 0)
        else:
            self.detail.setPlainText("No devices known yet.")

    @staticmethod
    def describe(card: dev.DeviceCard) -> str:
        lines = [card.headline, ""]
        if card.metrics:
            lines += [f"{k}: {v}" for k, v in card.metrics.items()] + [""]
        lines.append("Permissions: " + (", ".join(card.permissions) if card.permissions else "none granted / not applicable"))
        lines.append("")
        lines.append("What it can do right now:")
        lines += [f"  {c.glyph} {c.name}" + (f" - {c.why}" if c.why else "") for c in card.capabilities] or ["  nothing reported yet"]
        lines += ["", "Recent activity:", *[f"  {a}" for a in card.activity]]
        if card.warnings:
            lines += ["", *[f"⚠ {w}" for w in card.warnings]]
        lines += ["", "✔ available   ⚠ needs an action (shown)   ? not known yet"]
        return "\n".join(lines)

    def _show(self, row: int) -> None:
        if 0 <= row < len(self.cards):
            self.detail.setPlainText(self.describe(self.cards[row]))


# ------------------------------------------------------------------ security


class SecurityPage(QWidget):
    emergency_stop = Signal()

    def __init__(self, collect: Callable[[], sec.Overview], *, open_phones=None, open_privacy=None, open_emergency=None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.collect = collect
        self.headline = QLabel("")
        self.headline.setObjectName("h1")
        self.headline.setWordWrap(True)
        self.headline.setAccessibleName("Security summary")
        self.report = _text("Security report")
        self.stop_button = QPushButton("■  EMERGENCY STOP")
        self.stop_button.setObjectName("danger")
        self.stop_button.setToolTip("Stop everything Jarvis is doing now (also Ctrl+Shift+F12). It tells you what it "
                                    "stopped and what it could not.")
        self.stop_button.setAccessibleName("Emergency stop")
        self.stop_button.clicked.connect(self.emergency_stop.emit)
        row = QHBoxLayout()
        row.addWidget(_button("Refresh", "Check everything again (also verifies the activity log)", self.refresh))
        row.addWidget(_button("Phones...", "Trust, restrict or revoke; change what a phone may do", open_phones))
        row.addWidget(_button("Privacy...", "What Jarvis stores, and delete it", open_privacy))
        row.addWidget(_button("Emergency controls...", "Lock Jarvis, cut the network, stop automations", open_emergency))
        row.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(self.headline, 1)
        top.addWidget(self.stop_button)
        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Security center"))
        layout.addLayout(top)
        layout.addWidget(self.report, 1)
        layout.addLayout(row)

    def refresh(self) -> None:
        try:
            overview = self.collect()
        except Exception:
            overview = sec.Overview([], [])
        self.headline.setText(("■ " if overview.emergency else ("⚠ " if overview.warnings else "✔ ")) + overview.headline)
        out: list[str] = []
        for s in overview.sections:
            out.append(s.title.upper())
            out += [f"  {sec.LEVEL_GLYPH.get(i.level, '·')} {i.text}" for i in s.items]
            out.append("")
        out.append("✔ fine   ℹ worth knowing   ⚠ needs a look")
        self.report.setPlainText("\n".join(out))


# ------------------------------------------------------------------ automations


class AutomationsPage(QWidget):
    """Pick a routine -> see Trigger / Conditions / Actions / Approvals / If it fails, with warnings -> Test it
    (nothing runs) -> Simulate a what-if -> Enable. Editing steps opens the full Workflow studio."""

    def __init__(self, store, registry, ctx, permissions, *, open_studio=None, run=None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store, self.registry, self.ctx, self.permissions, self.run_now = store, registry, ctx, permissions, run
        self.tasks, self.info = [], None
        self.listing = QListWidget()
        self.listing.setAccessibleName("Routines")
        self.listing.currentRowChanged.connect(self._choose)
        self.view = _text("Routine details")
        self.output = _text("Test result")
        self.output.setMaximumHeight(150)
        self.what_if = QComboBox()
        self.what_if.setAccessibleName("What-if to simulate")
        from app.automation import simulator

        for key in simulator.WHAT_IFS:
            self.what_if.addItem(simulator.WHAT_IF_LABELS[key], key)
        self.ack = QCheckBox("I've read the warnings")
        self.ack.setAccessibleName("I have read the warnings")
        self.buttons = {
            "test": _button("Test (dry run)", "Show what each step would do now. Nothing is run.", self.test),
            "sim": _button("Simulate what-if", "Pretend tools on a scratch database. Nothing real runs.", self.simulate),
            "enable": _button("Enable", "Turn the routine on (refused while it has a real problem)", self.enable),
            "disable": _button("Disable", "Turn the routine off", self.disable),
            "run": _button("Run now", "Run it now (steps that need approval still ask)", self._run, "Run routine now"),
            "studio": _button("Edit steps...", "Reorder, add conditions, set error handling in the Workflow studio", open_studio),
        }
        row = QHBoxLayout()
        for b in self.buttons.values():
            row.addWidget(b)
        row.addStretch(1)
        sim_row = QHBoxLayout()
        sim_row.addWidget(QLabel("What if:"))
        sim_row.addWidget(self.what_if, 1)
        sim_row.addWidget(self.ack)
        right = QVBoxLayout()
        right.addWidget(self.view, 3)
        right.addLayout(sim_row)
        right.addWidget(self.output, 1)
        right.addLayout(row)
        body = QHBoxLayout()
        body.addWidget(self.listing, 2)
        body.addLayout(right, 5)
        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Automations"))
        layout.addLayout(body, 1)

    def refresh(self) -> None:
        keep = self.listing.currentRow()
        try:
            self.tasks = self.store.all()
        except Exception:
            self.tasks = []
        self.listing.clear()
        for t in self.tasks:
            self.listing.addItem(f"{'●' if t.enabled else '○'} {t.name}  ({'on' if t.enabled else 'off'}) - {t.trigger.describe()}")
        if self.tasks:
            self.listing.setCurrentRow(keep if 0 <= keep < len(self.tasks) else 0)
        else:
            self.view.setPlainText("No routines yet. Say 'every weekday at 8 open Chrome' or save a workflow, and it shows up here.")
            self._buttons_for(None)

    def _buttons_for(self, task) -> None:
        for key in ("test", "sim", "enable", "disable", "run", "studio"):
            self.buttons[key].setEnabled(task is not None and (key != "studio" or self.buttons["studio"].isEnabled()))
        if task is not None:
            self.buttons["enable"].setVisible(not task.enabled)
            self.buttons["disable"].setVisible(task.enabled)
            self.buttons["run"].setEnabled(self.run_now is not None)

    @property
    def task(self):
        row = self.listing.currentRow()
        return self.tasks[row] if 0 <= row < len(self.tasks) else None

    def _choose(self, row: int) -> None:
        task = self.task
        self.ack.setChecked(False)
        self.output.clear()
        if task is None:
            return
        self.info = auto.inspect(task, self.registry, self.ctx, self.permissions)
        self.view.setPlainText(self.describe(self.info))
        self._buttons_for(task)

    @staticmethod
    def describe(i: auto.Inspection) -> str:
        out = [f"{i.name}  -  {'ON' if i.enabled else 'OFF'}", "", "TRIGGER", f"  {i.trigger}", "", "CONDITIONS"]
        out += [f"  {c}" for c in i.conditions]
        out += ["", "ACTIONS"] + [f"  {a.n}. {a.text}   [{a.flags}]   on error: {a.on_error}" for a in i.actions]
        out += ["", "APPROVALS"] + ([f"  ⏸ {a}" for a in i.approvals] or ["  none: nothing asks before it runs"])
        out += ["", "IF IT FAILS", f"  {i.failure}"]
        if i.last_status:
            out += ["", f"Last run: {i.last_status}"]
        if i.warnings:
            out += ["", "WARNINGS"] + [f"  ⚠ {w}" for w in i.warnings]
        if i.problems:
            out += ["", "PROBLEMS (it can't be enabled until these are fixed)"] + [f"  ✖ {p}" for p in i.problems]
        return "\n".join(out)

    def test(self) -> str:
        task = self.task
        if task is None:
            return ""
        text = auto.test_text(task, self.registry, self.ctx, self.permissions)
        self.output.setPlainText(text)
        return text

    def simulate(self) -> str:
        task = self.task
        if task is None:
            return ""
        text = auto.simulate_text(task, self.what_if.currentData(), self.registry)
        self.output.setPlainText(text)
        return text

    def enable(self) -> tuple[bool, str]:
        task = self.task
        if task is None or self.info is None:
            return False, ""
        ok, message = auto.enable(self.store, task, self.info, acknowledged=self.ack.isChecked())
        self.output.setPlainText(message)
        if ok:
            self.refresh()
        return ok, message

    def disable(self) -> str:
        task = self.task
        if task is None:
            return ""
        message = auto.disable(self.store, task)
        self.output.setPlainText(message)
        self.refresh()
        return message

    def _run(self) -> None:
        task = self.task
        if task is not None and self.run_now is not None:
            self.run_now(f"run {task.name}")


# ------------------------------------------------------------------ tasks, dashboard, launchers


class TasksPage(QWidget):
    def __init__(self, load: Callable[[], list], *, open_mission_control=None, open_background=None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.load = load
        self.listing = QListWidget()
        self.listing.setAccessibleName("Recent tasks")
        self.listing.currentRowChanged.connect(self._show)
        self.detail = _text("Task timeline")
        self.missions: list = []
        row = QHBoxLayout()
        row.addWidget(_button("Refresh", "Read the tasks again", self.refresh))
        row.addWidget(_button("Mission Control...", "Every request, step by step, and what can be undone", open_mission_control))
        row.addWidget(_button("Background tasks...", "What is running behind the scenes", open_background))
        row.addStretch(1)
        body = QHBoxLayout()
        body.addWidget(self.listing, 2)
        body.addWidget(self.detail, 4)
        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Tasks"))
        layout.addLayout(body, 1)
        layout.addLayout(row)

    def refresh(self) -> None:
        try:
            self.missions = self.load()
        except Exception:
            self.missions = []
        self.listing.clear()
        for m in self.missions:
            tl = timeline.build(m)
            self.listing.addItem(f"{tl.state_label}: {tl.title} ({tl.progress})")
        if self.missions:
            self.listing.setCurrentRow(0)
        else:
            self.detail.setPlainText("No multi-step tasks yet. Ask for something with several steps, e.g. 'prepare "
                                     "everything for tomorrow's presentation'.")

    def _show(self, row: int) -> None:
        if 0 <= row < len(self.missions):
            tl = timeline.build(self.missions[row])
            lines = [tl.summary(), ""] + [r.text for r in tl.rows]
            c = tl.controls
            lines += ["", "Available now: " + ", ".join(k for k, v in (("cancel", c.cancel), ("retry", c.retry),
                                                                    ("undo", c.undo), ("review", c.review)) if v)]
            self.detail.setPlainText("\n".join(lines))


class LauncherPage(QWidget):
    """A page with a real summary and the buttons for what you can do here (each opens the full existing screen)."""

    def __init__(self, title: str, summary: Callable[[], str], actions: list[tuple[str, str, Callable[[], None] | None]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.summary_fn = summary
        self.summary = _text(f"{title} summary")
        grid = QGridLayout()
        for i, (label, tip, slot) in enumerate(actions):
            grid.addWidget(_button(label, tip, slot), i // 3, i % 3)
        layout = QVBoxLayout(self)
        layout.addWidget(_heading(title))
        layout.addWidget(self.summary, 1)
        layout.addLayout(grid)

    def refresh(self) -> None:
        try:
            self.summary.setPlainText(self.summary_fn())
        except Exception:
            self.summary.setPlainText("Not available right now.")


class KnowledgePage(QWidget):
    """Browse the knowledge graph (projects, technologies, files, memories, decisions, goals...) and what connects them.

    The graph is built in a background thread when the page is first shown or when you press Rebuild, so the window
    never freezes; it only reads what Jarvis already stores."""

    built = Signal(object)

    def __init__(self, build: Callable[[], object], summary: Callable[[], str], ask: Callable[[str], str],
                 open_documents: Callable[[], None] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build, self._summary_fn, self._ask = build, summary, ask
        self.graph = None
        self._loading = False
        self.built.connect(self._on_built)
        self.status = QLabel("Not loaded yet.")
        self.status.setObjectName("muted")
        self.status.setAccessibleName("Graph status")
        self.kind = QComboBox()
        self.kind.setAccessibleName("Show only this kind")
        self.kind.currentIndexChanged.connect(self._fill)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search names")
        self.search.setAccessibleName("Search the graph")
        self.search.textChanged.connect(self._fill)
        self.rebuild = _button("Rebuild", "Read the graph again from your projects, memories and goals", self.load)
        self.listing = QListWidget()
        self.listing.setAccessibleName("Graph items")
        self.listing.currentItemChanged.connect(self._show)
        self.detail = _text("Item details")
        self.detail.setPlainText("Select an item to see how it connects.")
        self.ask_box = QLineEdit()
        self.ask_box.setPlaceholderText("Ask: which files are related to ..., what did I decide about ...")
        self.ask_box.setAccessibleName("Ask the knowledge graph")
        self.ask_box.returnPressed.connect(self._do_ask)
        self.answer = _text("Graph answer")
        self.docs = QLabel("")
        self.docs.setObjectName("muted")
        self.docs.setAccessibleName("Indexed documents")
        top = QHBoxLayout()
        top.addWidget(self.kind)
        top.addWidget(self.search, 1)
        top.addWidget(self.rebuild)
        left = QVBoxLayout()
        left.addLayout(top)
        left.addWidget(self.listing, 1)
        right = QVBoxLayout()
        right.addWidget(self.detail, 2)
        right.addWidget(self.ask_box)
        right.addWidget(self.answer, 1)
        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addLayout(right, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Knowledge"))
        layout.addWidget(self.status)
        layout.addLayout(body, 1)
        foot = QHBoxLayout()
        foot.addWidget(self.docs, 1)
        foot.addWidget(_button("Documents...", "Index and ask your documents", open_documents))
        layout.addLayout(foot)

    def refresh(self) -> None:
        try:
            self.docs.setText(" ".join(self._summary_fn().splitlines()[2:4]) or "")
        except Exception:
            self.docs.setText("")
        if self.graph is None and not self._loading:
            self.load()

    def load(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.status.setText("Reading projects, memories and goals...")
        import threading

        def work() -> None:
            try:
                result = self._build()
            except Exception:
                result = None
            self.built.emit(result)

        threading.Thread(target=work, daemon=True, name="graph-build").start()

    def _on_built(self, graph) -> None:
        self._loading = False
        if graph is None:
            self.status.setText("The graph could not be read right now.")
            return
        self.graph = graph
        self.kind.blockSignals(True)
        self.kind.clear()
        self.kind.addItem(f"All ({len(graph.nodes)})", "")
        for key, label, count in gv.kinds(graph):
            self.kind.addItem(f"{label} ({count})", key)
        self.kind.blockSignals(False)
        self.status.setText(f"{len(graph.nodes)} items, {len(graph.edges)} connections, built from data on this PC.")
        self._fill()

    def _fill(self) -> None:
        if self.graph is None:
            return
        found, total = gv.rows(self.graph, self.kind.currentData() or "", self.search.text())
        self.listing.clear()
        for row in found:
            item = QListWidgetItem(f"{row.name}   ({row.links})" if row.links else row.name)
            item.setData(Qt.ItemDataRole.UserRole, row.id)
            item.setToolTip(f"{gv.KIND_LABEL.get(row.kind, row.kind)}; {row.links} connection(s)")
            self.listing.addItem(item)
        if total > len(found):
            self.status.setText(self.status.text().split("  Showing")[0] + f"  Showing the {len(found)} most connected of {total}.")

    def _show(self, item, _previous=None) -> None:
        if item is None or self.graph is None:
            return
        self.detail.setPlainText(gv.details(self.graph, item.data(Qt.ItemDataRole.UserRole)))

    def _do_ask(self) -> None:
        question = self.ask_box.text().strip()
        if question:
            self.answer.setPlainText("Looking...")
            QTimer.singleShot(0, lambda: self.answer.setPlainText(self._ask(question)))


class IntelligencePage(QWidget):
    """The Intelligence Center: eight panels, each a real read or write through the normal tool path (so permissions,
    confirmations and the audit log apply exactly as if you had asked in chat). Concise by default; tick "Show full
    detail" for the whole answer."""

    finished = Signal(str)
    CONCISE_LINES = 12

    PANELS = ("Understand a request", "Decisions", "Preferences", "Investigate the AI model", "Research brief", "Goals",
              "Alerts (radar)", "Capabilities", "Unusual for you", "Routines you repeat", "Task replay", "Digital twin")

    def __init__(self, run_tool: Callable[[str, dict, Callable[[str], None]], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._run = run_tool
        self._full = ""
        self.finished.connect(self._show)
        self.menu = QListWidget()
        self.menu.setFixedWidth(210)
        self.menu.setAccessibleName("Intelligence panels")
        self.menu.addItems(list(self.PANELS))
        self.stack = QStackedWidget()
        self.output = _text("Result")
        self.detail = QCheckBox("Show full detail")
        self.detail.setToolTip("Concise: the first lines. Detailed: the whole answer.")
        self.detail.setAccessibleName("Show full detail")
        self.detail.toggled.connect(lambda _=False: self._render())
        self.fields: dict[str, QLineEdit] = {}
        self.stack.addWidget(self._panel_understand())
        self.stack.addWidget(self._panel_decisions())
        self.stack.addWidget(self._panel_preferences())
        self.stack.addWidget(self._panel_investigate())
        self.stack.addWidget(self._panel_brief())
        self.stack.addWidget(self._panel_goals())
        self.stack.addWidget(self._panel_radar())
        self.stack.addWidget(self._panel_capabilities())
        self.stack.addWidget(self._panel_anomalies())
        self.stack.addWidget(self._panel_routines())
        self.stack.addWidget(self._panel_replay())
        self.stack.addWidget(self._panel_twin())
        self.menu.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.menu.setCurrentRow(0)
        right = QVBoxLayout()
        right.addWidget(self.stack)
        right.addWidget(self.detail)
        right.addWidget(self.output, 1)
        body = QHBoxLayout()
        body.addWidget(self.menu)
        body.addLayout(right, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Intelligence"))
        note = QLabel("Everything here is computed on this PC from your own data. Estimates are labelled; nothing changes "
                      "without the same approvals as in chat.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addLayout(body, 1)

    # ----------------------------------------------------------------- helpers
    def _field(self, key: str, placeholder: str) -> QLineEdit:
        box = QLineEdit()
        box.setPlaceholderText(placeholder)
        box.setAccessibleName(placeholder)
        self.fields[key] = box
        return box

    def _call(self, tool: str, arguments: dict | Callable[[], dict]) -> Callable[[], None]:
        def go() -> None:
            args = arguments() if callable(arguments) else arguments
            self.output.setPlainText("Working...")
            self._run(tool, args, self.finished.emit)
        return go

    def _panel(self, hint: str, *rows: list[QWidget]) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        label = QLabel(hint)
        label.setObjectName("muted")
        label.setWordWrap(True)
        lay.addWidget(label)
        for row in rows:
            h = QHBoxLayout()
            for widget in row:
                h.addWidget(widget, 1 if isinstance(widget, QLineEdit) else 0)
            lay.addLayout(h)
        return w

    def _btn(self, label: str, tip: str, tool: str, arguments: dict | Callable[[], dict]) -> QPushButton:
        return _button(label, tip, self._call(tool, arguments))

    def text(self, key: str) -> str:
        return self.fields[key].text().strip()

    # ----------------------------------------------------------------- panels
    def _panel_understand(self) -> QWidget:
        f = self._field("request", "What do you want done?")
        return self._panel("What Jarvis understood, what would count as done, what it assumes, and what it needs to ask.",
                           [f, self._btn("Understand", "Compile this request into a goal", "goal_check", lambda: {"request": self.text("request")})],
                           [self._btn("Check the last mission", "Did the latest mission really meet its goal?", "goal_check", {})])

    def _panel_decisions(self) -> QWidget:
        return self._panel("Decisions you recorded, with alternatives and outcomes. They are notes, never rules.",
                           [self._field("d_title", "Decision title"), self._field("d_chosen", "What was chosen")],
                           [self._btn("Record", "Save this decision", "decision", lambda: {"action": "record", "title": self.text("d_title"), "chosen": self.text("d_chosen")}),
                            self._btn("List", "All recorded decisions", "decision", {"action": "list"}),
                            self._field("d_topic", "Topic"),
                            self._btn("Find earlier", "Earlier decisions about this topic", "decision", lambda: {"action": "relevant", "topic": self.text("d_topic")})])

    def _panel_preferences(self) -> QWidget:
        combo = QComboBox()
        combo.addItems(["general", "development", "study", "research", "planning", "gaming"])
        combo.setAccessibleName("Profile")
        self.profile = combo
        return self._panel("Preferences you stated or that Jarvis noticed. Noticed ones are never used until you confirm them. "
                           "Nothing here can turn off approvals.",
                           [self._field("p_text", "Say it, e.g. keep responses shorter"),
                            self._btn("Remember", "Save this preference", "preferences", lambda: {"action": "learn", "text": self.text("p_text")})],
                           [self._btn("List", "All preferences", "preferences", {"action": "list"}),
                            self._btn("Questions", "Patterns worth asking you about", "preferences", {"action": "questions"}),
                            combo, self._btn("What applies", "What is in force for this profile", "preferences", lambda: {"action": "resolve", "profile": self.profile.currentText()}),
                            self._btn("Learning on", "Allow noticing preferences", "preferences", {"action": "adaptation", "on": True}),
                            self._btn("Learning off", "Use only what you said and the defaults", "preferences", {"action": "adaptation", "on": False})])

    def _panel_investigate(self) -> QWidget:
        return self._panel("Why isn't the local AI model answering? Checks the cheapest evidence first and stops when a cause is found.",
                           [self._btn("Investigate", "Check Ollama, the model, memory and recent errors", "investigate", {}),
                            self._btn("Check every cause", "Do not stop at the first cause", "investigate", {"keep_going": True})])

    def _panel_brief(self) -> QWidget:
        return self._panel("A brief from your own memories, decisions, documents and graph - offline, with sources.",
                           [self._field("b_q", "Question or topic"),
                            self._btn("Brief", "Build a sourced brief", "research_brief", lambda: {"action": "brief", "question": self.text("b_q")})],
                           [self._btn("Documentation gaps", "What is missing or unexplained", "research_brief", {"action": "gaps"}),
                            self._btn("Proposed links", "Possible graph relationships (unconfirmed)", "research_brief", {"action": "proposals"})])

    def _panel_goals(self) -> QWidget:
        return self._panel("Progress from finished tasks, your real pace, a rough forecast and proposed replans. Nothing is edited.",
                           [self._btn("Assess goals", "Progress and forecast per goal", "goal_review", {"action": "assess"}),
                            self._btn("Propose replans", "Suggested changes for goals that are behind", "goal_review", {"action": "replan"}),
                            self._btn("Weekly review", "What happened this week", "goal_review", {"action": "review"})])

    def _panel_radar(self) -> QWidget:
        k = self._field("r_key", "Alert key (e.g. resource:ram)")

        def fb(action: str) -> Callable[[], dict]:
            return lambda: {"action": action, "key": self.text("r_key")}
        return self._panel("What Jarvis would raise right now, and how you tell it about an alert.",
                           [self._btn("Scan now", "Local resource and device checks", "radar_feedback", {"action": "scan"})],
                           [k, self._btn("Snooze 24 h", "Hold it back for a day", "radar_feedback", fb("snooze")),
                            self._btn("Mute", "Never raise it", "radar_feedback", fb("mute")),
                            self._btn("Unmute", "Allow it again", "radar_feedback", fb("unmute"))],
                           [self._btn("Useful", "This alert helped", "radar_feedback", fb("useful")),
                            self._btn("Not useful", "Three of these mute it for a week", "radar_feedback", fb("not_useful"))])

    def _panel_capabilities(self) -> QWidget:
        return self._panel("What is really missing: recurring failures by cause, and the tool inventory.",
                           [self._btn("What's missing?", "Failures classified by cause, with proposals", "capability_report", {"action": "gaps"}),
                            self._btn("Tool inventory", "Risk, tests and failure rates", "capability_report", {"action": "inventory"})])

    def _panel_anomalies(self) -> QWidget:
        return self._panel("What looks statistically unusual against your own history (slow or failing tools, PC load, "
                           "spending). Too little history means no verdict.",
                           [self._btn("Check now", "Median / MAD against your own last 30 days", "anomalies", {})])

    def _panel_routines(self) -> QWidget:
        n = self._field("x_id", "Proposal number or id")
        return self._panel("Routines you repeat, found in Jarvis's own record of what it did - never your keyboard, mouse or "
                           "screen. Saving asks you first.",
                           [self._btn("Find routines", "Same successful steps, same order, on 2+ days", "experience", {"action": "propose"})],
                           [n, self._btn("Save as workflow", "Asks for confirmation", "experience",
                                         lambda: {"action": "save", "id": self.text("x_id")}),
                            self._btn("Don't offer again", "Dismiss this proposal", "experience",
                                      lambda: {"action": "dismiss", "id": self.text("x_id")})])

    def _panel_twin(self) -> QWidget:
        m = self._field("p_twin_mission", "Mission id (to preview its remaining steps)")
        return self._panel("What Jarvis last observed about your devices, missions, disk and memory, each with its source and age (old "
                           "readings are marked STALE; unobserved things are UNKNOWN). The preview estimates what a mission would touch "
                           "before it runs; nothing is run.",
                           [self._btn("Show", "Observations with source and age", "digital_twin", {"action": "show"}),
                            self._btn("Refresh", "Observe devices, missions, disk and memory now (local reads only)", "digital_twin", {"action": "refresh"})],
                           [m, self._btn("Preview mission", "Risk, touched paths, collisions, undo, likely failures - nothing runs",
                                         "digital_twin", lambda: {"action": "what_if", "mission_id": self.text("p_twin_mission")})],
                           [self._btn("Forget observations", "Clear the twin", "digital_twin", {"action": "forget"})])

    def _panel_replay(self) -> QWidget:
        r = self._field("p_run", "Run id (empty = latest that went wrong)")
        c = self._field("p_call", "Call number (for live)")
        t = self._field("p_tool", "Tool name (for hide / show details)")

        def a(action: str) -> Callable[[], dict]:
            def build() -> dict:
                data: dict = {"action": action, "run_id": self.text("p_run")}
                if action == "live":
                    data["call"] = int(self.text("p_call") or 1) if (self.text("p_call") or "1").isdigit() else 1
                if action in ("hide", "unhide"):
                    data["tool"] = self.text("p_tool")
                return data
            return build
        return self._panel("Why a task went wrong, from Jarvis's own record: facts are marked recorded or reconstructed, missing "
                           "data is named. Simulation uses mock tools. Live re-runs only read-only calls and asks first.",
                           [self._btn("Runs that went wrong", "Recent failed or diverging runs", "task_replay", {"action": "list"}),
                            self._btn("Diagnose", "Earliest divergence, likely cause, minimal recovery", "task_replay", a("diagnose")),
                            self._btn("Timeline", "Every recorded step", "task_replay", a("show"))],
                           [r, self._btn("Simulate", "Mock tools + recorded results; nothing external runs", "task_replay", a("simulate")),
                            c, self._btn("Live re-run", "One read-only call, checked and confirmed now", "task_replay", a("live"))],
                           [t, self._btn("Hide details", "Hide this tool's arguments and results in replays", "task_replay", a("hide")),
                            self._btn("Show details", "Stop hiding (asks first)", "task_replay", a("unhide"))])

    # ----------------------------------------------------------------- output
    def _show(self, text: str) -> None:
        self._full = text
        self._render()

    def _render(self) -> None:
        lines = self._full.splitlines()
        if self.detail.isChecked() or len(lines) <= self.CONCISE_LINES:
            self.output.setPlainText(self._full)
        else:
            more = len(lines) - self.CONCISE_LINES
            self.output.setPlainText("\n".join(lines[:self.CONCISE_LINES]) + f"\n... ({more} more lines: tick Show full detail)")
