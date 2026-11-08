"""CommandCenterView: the new main working view. Left rail | (page) | context panel, with the global command bar and a
compact status area underneath. It sits beside the 3D core view (Ctrl+Shift+C / Ctrl+Shift+H) and talks to the same
controller, so everything Jarvis does is visible in both."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QSplitter, QStackedWidget,
                               QVBoxLayout, QWidget)

from app.core import activity, emergency
from app.ui.chat import ChatView
from app.ui.command_center import devices, pages, security, states, style, timeline
from app.ui.command_center.approval import describe as describe_approval
from app.ui.command_center.context_panel import ContextPanel
from app.ui.command_center.widgets import CommandBar, Rail, StatusArea

TEXT_SIZES = (("Small", 0.9), ("Normal", 1.0), ("Large", 1.25), ("Extra large", 1.5))
OUTCOME_SECONDS = 6


class SettingsQuick(QWidget):
    """The settings people change most, applied at once; everything else is one button away."""

    def __init__(self, apply: Callable[[str, str], None], settings, open_all: Callable[[], None] | None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.apply = apply
        title = QLabel("Settings")
        title.setObjectName("h1")
        self.theme = QComboBox()
        self.theme.setAccessibleName("Theme")
        self.theme.addItems(["dark", "light", "forest"])
        self.theme.setCurrentText(getattr(settings, "ui_theme", "dark"))
        self.size = QComboBox()
        self.size.setAccessibleName("Text size")
        scale = float(getattr(settings, "ui_text_scale", 1.0))
        for label, value in TEXT_SIZES:
            self.size.addItem(label, value)
        self.size.setCurrentIndex(min(range(len(TEXT_SIZES)), key=lambda i: abs(TEXT_SIZES[i][1] - scale)))
        self.motion = QCheckBox("Reduced motion (calmer animation)")
        self.motion.setChecked(bool(getattr(settings, "reduced_motion", False)))
        self.theme.currentTextChanged.connect(lambda v: self.apply("ui_theme", v))
        self.size.currentIndexChanged.connect(lambda _i: self.apply("ui_text_scale", str(self.size.currentData())))
        self.motion.toggled.connect(lambda on: self.apply("reduced_motion", "true" if on else "false"))
        all_button = QPushButton("All settings...")
        all_button.setToolTip("Voice, AI models, privacy, remote access and everything else")
        all_button.setEnabled(open_all is not None)
        if open_all is not None:
            all_button.clicked.connect(lambda: open_all())
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        for label, widget in (("Theme", self.theme), ("Text size", self.size)):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(widget)
            row.addStretch(1)
            layout.addLayout(row)
        layout.addWidget(self.motion)
        layout.addWidget(all_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)


class CommandCenterView(QWidget):
    core_requested = Signal()

    def __init__(self, controller, window=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ccRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.controller = controller
        self.window_ = window
        settings = controller.ctx.settings
        self.rail = Rail()
        self.status = StatusArea()
        self.command = CommandBar()
        self.context = ContextPanel()
        self.chat = ChatView(settings.assistant_name)
        self.chat.setStyleSheet("QTextBrowser { background:#0a1626; color:#e6eef8; border:1px solid #2a3b57; border-radius:8px; }")
        self._outcome: str | None = None
        self._ai_ok: bool | None = None
        self._busy_text = ""
        self._metrics: dict[str, object] = {}
        self._cards_cache: list[devices.DeviceCard] = []

        self.pages = QStackedWidget()
        self.page_index: dict[str, int] = {}
        chat_page = QSplitter(Qt.Orientation.Horizontal)
        chat_page.addWidget(self.chat)
        chat_page.addWidget(self.context)
        chat_page.setStretchFactor(0, 3)
        chat_page.setStretchFactor(1, 1)
        chat_page.setChildrenCollapsible(False)
        self._add("chat", chat_page)
        from app.ui.command_center.dashboard_page import DashboardPage

        self.dashboard = DashboardPage(self._dashboard_cards, [
            ("Core view", "The 3D core", self.core_requested.emit),
            ("Notifications...", "What Jarvis told you", self._w("open_notifications")),
            ("My day...", "To-dos, plan, history", self._w("open_my_day")),
            ("Workspaces...", "Every screen (Ctrl+K)", self._w("open_workspaces"))])
        self._add("dashboard", self.dashboard)
        self.tasks = pages.TasksPage(self._missions, open_mission_control=self._w("open_mission_control"),
                                     open_background=self._w("open_task_center"))
        self._add("tasks", self.tasks)
        self.devices = pages.DevicesPage(self._collect_devices, open_phones=self._w("open_mobile_devices"),
                                         open_manager=self._w("open_device_manager"),
                                         open_phone_screen=self._w("open_phone_screen"))
        self._add("devices", self.devices)
        self.memory = pages.LauncherPage("Memory", self._memory_text, [
            ("Open memory...", "See, edit and delete what Jarvis remembers", self._w("open_memory")),
            ("Privacy dashboard...", "What is stored on this PC", self._w("open_privacy")),
            ("What Jarvis sees...", "The context it uses right now", self._w("open_context_panel"))])
        self._add("memory", self.memory)
        self.knowledge = pages.KnowledgePage(self._build_graph, self._knowledge_text, self._ask_graph,
                                             open_documents=self._w("open_knowledge"))
        self._add("knowledge", self.knowledge)
        self.automations = self._make_automations()
        self._add("automations", self.automations)
        self.developer = pages.LauncherPage("Developer Tools", self._developer_text, [
            ("Project status", "git status of the current project", lambda: self._send("git status")),
            ("Check project", "Run the project's checks", lambda: self._send("check my project")),
            ("Logs...", "The technical record", self._w("open_logs")),
            ("Diagnostics...", "Health and recovery", self._w("open_diagnostics")),
            ("Evolution center...", "Improvements Jarvis proposes", self._w("open_evolution_center")),
            ("Workflow studio...", "Edit multi-step workflows", self._w("open_workflow_studio"))])
        self._add("developer", self.developer)
        self.settings_page = SettingsQuick(self._change_setting, settings, self._w("open_settings"))
        self._add("settings", self.settings_page)
        self.security = pages.SecurityPage(self._security_overview, open_phones=self._w("open_mobile_devices"),
                                           open_privacy=self._w("open_privacy"), open_emergency=self._w("open_emergency"))
        self.security.emergency_stop.connect(self._emergency)
        self._add("security", self.security)
        self.intelligence = pages.IntelligencePage(self._run_tool)
        self._add("intelligence", self.intelligence)
        from app.ui.command_center.handoff_page import HandoffPage

        self.handoff = HandoffPage(self._handoff_rows, self._trusted_phones, self._run_tool)
        self._add("handoff", self.handoff)
        from app.ui.command_center import panel_specs
        from app.ui.command_center.tool_panel import ToolPanelPage

        self.tool_pages: dict[str, ToolPanelPage] = {}
        for spec in panel_specs.PANELS:
            page = ToolPanelPage(spec, self._run_tool, {name: self._w(name) for name in spec.extras if self._w(name) is not None})
            self.tool_pages[spec.key] = page
            self._add(spec.key, page)
        from app.ui.command_center.gesture_page import GesturePage

        self.gestures = GesturePage(self._run_tool, lambda: self.controller.ctx)
        self._add("gestures", self.gestures)

        right = QVBoxLayout()
        right.setContentsMargins(12, 12, 12, 8)
        right.setSpacing(8)
        right.addWidget(self.pages, 1)
        right.addWidget(self.command)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.rail)
        body.addLayout(right, 1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(body, 1)
        outer.addWidget(self.status)

        self.rail.selected.connect(self.show_section)
        self.rail.core_button.clicked.connect(self.core_requested.emit)
        self.command.submitted.connect(self._send_command)
        self.command.mic.clicked.connect(controller.toggle_listening)
        self.command.stop.clicked.connect(controller.cancel)
        self.context.task.command.connect(self._send)
        self.context.task.review_requested.connect(self._w("open_mission_control") or (lambda: None))
        self.context.task.mission_request.connect(self._mission_request)
        self._bind(controller)
        self.apply_style()
        self._timer = QTimer(self)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self.tick)
        self.tick()

    # ----------------------------------------------------------------- wiring
    def _add(self, key: str, widget: QWidget) -> None:
        self.page_index[key] = self.pages.addWidget(widget)

    def _w(self, name: str) -> Callable[[], None] | None:
        method = getattr(self.window_, name, None)
        return method if callable(method) else None

    def _bind(self, c) -> None:
        c.user_message.connect(lambda text: self.chat.add_message("user", text, c.spoken_wave()))
        c.assistant_started.connect(self.chat.begin_stream)
        c.assistant_delta.connect(self.chat.append_stream)
        c.assistant_finished.connect(self._on_finished)
        c.tool_event.connect(self._on_tool_event)
        c.notice.connect(lambda text: self.chat.add_message("notice", text))
        c.state_changed.connect(lambda _s: self.tick())
        c.status_changed.connect(self._on_status)
        c.confirmation_requested.connect(self._on_confirmation)
        c.confirmation_answered.connect(lambda *_: self.context.task.show_approval(None))
        c.mic_muted_changed.connect(lambda _m: self.tick())
        c.hands_free_state.connect(lambda _s: self.tick())

    def _on_status(self, status) -> None:
        self._ai_ok = bool(getattr(status, "reachable", False)) and getattr(status, "selected", None) is not None
        selected = getattr(status, "selected", None)
        self.status.set_all(model=f"AI: {selected.name}" if self._ai_ok and selected else "AI: offline")
        self.tick()

    def _on_finished(self, reply) -> None:
        self.chat.finish_reply(reply.text)
        self.context.show_sources(getattr(reply, "sources", None))
        self._outcome = "completed" if getattr(reply, "ok", True) else "failed"
        QTimer.singleShot(OUTCOME_SECONDS * 1000, self._clear_outcome)
        self.tick()

    def _clear_outcome(self) -> None:
        self._outcome = None
        self.tick()

    def _on_tool_event(self, text: str) -> None:
        self.context.details.append(text)
        name = text.split("]", 1)[-1].strip().split("(", 1)[0].strip()
        self._busy_text = name[:40]
        self.chat.add_message("tool", text)

    def _on_confirmation(self, pending) -> None:
        self.context.task.show_approval(describe_approval(pending.request))
        self.context.tabs.setCurrentIndex(0)

    def _emergency(self) -> None:
        try:
            self.chat.add_message("notice", self.controller.emergency_stop())
        except Exception:
            self.chat.add_message("notice", "The emergency stop could not run; use Ctrl+Shift+F12.")
        self.tick()

    def _run_tool(self, tool: str, arguments: dict, emit: Callable[[str], None]) -> None:
        """A button is your own command: the same checks, confirmations and audit as saying it (user_command=True)."""
        from app.ai.base import ToolCall

        c = self.controller

        def finished(future) -> None:
            try:
                result = future.result().result
                emit(result.message or result.error or "No answer.")
            except Exception as error:
                emit(f"Could not run that: {type(error).__name__}")

        try:
            future = c.runner.submit(c.executor.execute(ToolCall(tool, arguments), user_command=True))
            future.add_done_callback(finished)
        except Exception as error:
            emit(f"Could not run that: {type(error).__name__}")

    def _mission_request(self, mission_id: str, kind: str) -> None:
        from app.core import missions

        missions.request(mission_id, kind)
        self.chat.add_message("notice", f"{'Pausing' if kind == 'pause' else 'Cancelling'} before the next step.")

    # ----------------------------------------------------------------- actions
    def _send_command(self, text: str) -> None:
        if self.controller.send_text(text):
            self.command.accepted()

    def _send(self, text: str) -> None:
        self.controller.send_text(text)

    def _prefill(self, text: str) -> None:
        self.rail.select("chat")
        self.command.input.setText(text)
        self.command.focus()

    def _change_setting(self, name: str, value: str) -> None:
        self.controller._apply_setting(name, value)

    # ------------------------------------------------------------------- data
    def _missions(self) -> list:
        from app.core.missions import MissionStore

        db = self.controller.ctx.database
        return MissionStore(db).recent(15) if db is not None else []

    def _collect_devices(self) -> list[devices.DeviceCard]:
        self._cards_cache = devices.collect(self.controller, self._device_metrics())
        return self._cards_cache

    def _device_metrics(self) -> dict[str, str]:
        out = {}
        if isinstance(self._metrics.get("cpu"), (int, float)):
            out["CPU"] = f"{self._metrics['cpu']:.0f}%"
        if isinstance(self._metrics.get("ram"), (int, float)):
            out["Memory"] = f"{self._metrics['ram']:.0f}%"
        if self._metrics.get("gpu"):
            out["GPU"] = str(self._metrics["gpu"])
        return out

    def _security_overview(self) -> security.Overview:
        c = self.controller
        live = c.live_privacy_state() if hasattr(c, "live_privacy_state") else None
        return security.collect(c.ctx.database, c.ctx.settings, live)

    def _make_automations(self) -> QWidget:
        c = self.controller
        if c.ctx.database is None:
            return pages.LauncherPage("Automations", lambda: "The local database isn't available.", [])
        from app.automation.tasks import AutomationStore

        ctx = c.executor.context
        return pages.AutomationsPage(AutomationStore(c.ctx.database), ctx.get_tool_registry(), ctx,
                                     c.executor.permissions, open_studio=self._w("open_workflow_studio"),
                                     run=c.send_text)

    def _count(self, sql: str, *params) -> int | None:
        db = self.controller.ctx.database
        if db is None:
            return None
        try:
            with db.connect() as conn:
                return int(conn.execute(sql, params).fetchone()[0])
        except Exception:
            return None

    def _dashboard_cards(self) -> list:
        from app.ui.command_center import dashboard_data

        c = self.controller
        info = states.STATES[self.current_state().key]
        db = c.ctx.database
        perf_plan = gest = mission = pending = None
        try:
            from app.core import perf

            perf_plan = perf.current_plan(c.ctx.settings)
        except Exception:
            pass
        try:
            from app.gesture.service import SERVICE

            gest = SERVICE.status()
        except Exception:
            pass
        unfinished = self._count("SELECT COUNT(*) FROM missions WHERE state IN ('planned','running','paused','verifying','waiting')")
        try:
            from app.core.missions import MissionStore

            m = MissionStore(db).unfinished() if db is not None else None
            if m is not None and m.state in ("running", "verifying"):
                mission = {"title": m.title, "progress": m.progress(), "uncertain": [s.title for s in m.steps if s.state == "uncertain"]}
        except Exception:
            pass
        try:
            req = getattr(c, "_pending_confirmation", None)
            view = describe_approval(req.request) if req is not None else None
            pending = f"{view.action} ({view.risk} risk)" if view is not None else ""
        except Exception:
            pending = ""
        try:
            from app.core.action_record import LEDGER

            actions = [r.public() for r in LEDGER.recent(10)]
        except Exception:
            actions = []
        labels = self.status.labels
        cards = self._cards_cache or None
        return dashboard_data.build(
            state_text=info.text, model=labels["model"].text(), mic=labels["mic"].text(), network=labels["network"].text(),
            resources=f"{labels['cpu'].text()}   {labels['ram'].text()}   {labels['gpu'].text()}", gesture=gest, perf=perf_plan,
            mission=mission, unfinished=unfinished, pending_approval=pending or "", actions=actions, devices=cards,
            emergency=emergency.STATE.active(), todos=self._count("SELECT COUNT(*) FROM todos WHERE done = 0"),
            routines=self._count("SELECT COUNT(*) FROM automation_tasks WHERE enabled = 1"))

    def _handoff_rows(self) -> list:
        from app.ui.command_center import handoff_data

        db = self.controller.ctx.database
        if db is None:
            return []
        titles = {m.id: m.title for m in self._missions()}
        return handoff_data.collect(db, device_names=dict(self._trusted_phones()), mission_titles=titles)

    def _trusted_phones(self) -> list[tuple[str, str]]:
        db = self.controller.ctx.database
        if db is None:
            return []
        try:
            from app.remote.mobile import MobileRegistry

            return [(d.id, d.name) for d in MobileRegistry(db).all() if d.trust == "trusted" and not d.revoked_at]
        except Exception:
            return []

    def _dashboard_text(self) -> str:
        info = states.STATES[self.current_state().key]
        cards = self._cards_cache or []
        connected = sum(1 for c in cards if c.connection in ("connected", "local"))
        unfinished = self._count("SELECT COUNT(*) FROM missions WHERE state IN ('planned','running','paused','verifying','waiting')")
        routines = self._count("SELECT COUNT(*) FROM automation_tasks WHERE enabled = 1")
        todos = self._count("SELECT COUNT(*) FROM todos WHERE done = 0")
        lines = [f"Jarvis: {info.text}", f"{self.status.labels['model'].text()}",
                 f"{self.status.labels['cpu'].text()}   {self.status.labels['ram'].text()}   {self.status.labels['gpu'].text()}",
                 f"{self.status.labels['mic'].text()}   {self.status.labels['network'].text()}", ""]
        for label, value in (("Unfinished tasks", unfinished), ("Routines switched on", routines),
                             ("Open to-dos", todos), ("Devices connected", connected if cards else None)):
            lines.append(f"{label}: {value if value is not None else 'not known yet'}")
        active = emergency.STATE.active()
        if active:
            lines += ["", "■ Emergency controls ON: " + ", ".join(active)]
        return "\n".join(lines)

    def _memory_text(self) -> str:
        n = self._count("SELECT COUNT(*) FROM memories")
        return (f"Saved memories: {n if n is not None else 'not known'}\n\nEverything Jarvis remembers is on this PC. "
                "Open Memory to read, edit or delete any of it, see why something was recalled, or turn remembering off "
                "(Private mode).")

    def _build_graph(self):
        from app.core import knowledge_graph

        return knowledge_graph.build(self.controller.ctx)

    def _ask_graph(self, question: str) -> str:
        from app.core import knowledge_graph

        try:
            text, evidence, _kind = knowledge_graph.ask(self.controller.ctx, question)
        except Exception:
            return "The graph could not answer that right now."
        return text + ("\n\nEvidence:\n" + "\n".join(f"  - {e}" for e in evidence) if evidence else "")

    def _knowledge_text(self) -> str:
        db = self.controller.ctx.database
        lines = ["Your indexed documents and notes (searched on this PC, nothing is uploaded):", ""]
        try:
            with db.connect() as conn:
                rows = conn.execute("SELECT kind, COUNT(DISTINCT source) FROM semantic_items GROUP BY kind").fetchall()
            lines += [f"  {r[0]}: {r[1]} file(s)/item(s)" for r in rows] or ["  nothing indexed yet"]
        except Exception:
            lines.append("  not available")
        lines += ["", "Ask in the command bar: 'what does the contract say about notice period?'"]
        return "\n".join(lines)

    def _developer_text(self) -> str:
        return ("Project helpers run through the normal checked tools (permissions and approvals apply):\n"
                "  Project status, Check project, Logs, Diagnostics, Evolution center (proposed code improvements are "
                "always tested and need your approval).\n\nSay 'open my project in the editor' or 'run the tests' any time.")

    # ------------------------------------------------------------------ state
    def current_state(self) -> states.StateInfo:
        c = self.controller
        return states.resolve(getattr(c, "state", "IDLE") if isinstance(getattr(c, "state", "IDLE"), str) else "IDLE",
                              ai_online=self._ai_ok, emergency=bool(emergency.STATE.active()),
                              verifying=activity.is_verifying(), outcome=self._outcome)

    def tick(self) -> None:
        info = self.current_state()
        self.status.set_state(info, style.palette(getattr(self.controller.ctx.settings, "ui_theme", "dark"))["text"])
        c = self.controller
        muted = getattr(c, "mic_muted", False)
        listening = getattr(c, "hands_free", None) is not None
        self.status.set_all(mic="Mic: muted" if muted else ("Mic: always listening" if listening else "Mic: push to talk"))
        try:
            from app.utils import network

            s = c.ctx.settings
            offline = network.is_offline_now(s)
            self.status.set_all(network="Network: offline mode" if s.forced_offline else ("Network: no internet" if offline else "Network: online"))
        except Exception:
            pass
        if self._cards_cache:
            n = sum(1 for d in self._cards_cache if d.connection in ("connected", "local"))
            self.status.set_all(devices=f"Devices: {n} connected")
        self.command.show_status(info, self._busy_text if info.busy else "")
        if self.pages.currentIndex() in (self.page_index["chat"], self.page_index["tasks"]):
            self.context.task.show_timeline(timeline.current_timeline(c.ctx.database))

    def on_stats(self, cpu: float | None, ram: float | None, gpu: str | None) -> None:
        self._metrics = {"cpu": cpu, "ram": ram, "gpu": gpu}
        self.status.set_all(cpu=cpu, ram=ram, gpu=gpu if gpu and gpu != "--" else None)

    # ---------------------------------------------------------------- display
    def apply_style(self) -> None:
        s = self.controller.ctx.settings
        self.setStyleSheet(style.stylesheet(getattr(s, "ui_theme", "dark"), float(getattr(s, "ui_text_scale", 1.0))))

    def show_section(self, key: str) -> None:
        if key not in self.page_index:
            return
        self.rail.buttons[key].setChecked(True)
        self.pages.setCurrentIndex(self.page_index[key])
        page = self.pages.currentWidget()
        for name in ("refresh",):
            fn = getattr(page, name, None)
            if callable(fn):
                fn()
        self.tick()

    def showEvent(self, event) -> None:  # noqa: N802
        self._timer.start()
        self.tick()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()                        # nothing runs while the view isn't on screen
        super().hideEvent(event)
