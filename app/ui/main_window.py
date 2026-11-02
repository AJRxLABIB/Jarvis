"""Main window, HUD style: SYSTEM panel (live PC details) | the core | COMMS (conversation)."""

from __future__ import annotations

from app.utils.timefmt import time12

import os
import platform
import socket
import sys
import time
from datetime import datetime

import psutil
from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QPushButton, QScrollArea, QStackedWidget, QStatusBar,
    QVBoxLayout,
    QWidget,
)

from app import __app_name__, __version__
from app.ai.manager import AIStatus
from app.ui.chat import ChatView
from app.ui.confirm_dialog import ConfirmDialog
from app.ui.controller import AssistantController, PendingConfirmation
from app.ui.dashboard import (
    BLUE, CYAN, DIM as DASH_DIM, GREEN, ORANGE, PURPLE, CoreGauge, DashPanel, EventLog, LevelBars,
    DonutGauge, FrequencyCard, LogoRing, ModuleRow, ShineTitle, SpectrumStrip, VerticalMeters, StatusCard, StatusRow, TabButton, ThinBar,
)
from app.ui.hud import HexBackground, HudPanel, MiniBar, SegmentBar, Sparkline, StatRow as HudStatRow
from app.ui.styles.theme import GOLD, STATE_COLORS, STATE_LABELS
from app.ui.styles.tokens import COLOR


def _panel() -> QFrame:
    frame = QFrame()
    frame.setObjectName("panel")
    return frame


class Indicator(QLabel):
    """A privacy indicator that is only visible while its activity is happening."""

    def __init__(self, text: str, color: str) -> None:
        super().__init__(text)
        self.setObjectName("badge")
        self.setStyleSheet(f"background:{color}; color:#04070d;")
        self.setVisible(False)


def _human_rate(bytes_per_second: float) -> str:
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bytes_per_second < 1024 or unit == "GB/s":
            return f"{bytes_per_second:.0f} {unit}" if unit == "B/s" else f"{bytes_per_second:.1f} {unit}"
        bytes_per_second /= 1024
    return ""


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 60 * 24)
    hours, minutes = divmod(minutes, 60)
    return f"{days}d {hours}h {minutes:02d}m" if days else f"{hours}h {minutes:02d}m"


def _system_drive() -> str:
    return (os.environ.get("SystemDrive", "C:") + "\\") if sys.platform == "win32" else "/"


class MainWindow(QMainWindow):
    hidden_to_tray = Signal()
    stats_ready = Signal(object)  # a stats_sampler.Sample, measured in a worker thread

    def __init__(self, controller: AssistantController, monitor_interval: float = 3.0) -> None:
        super().__init__()
        self.controller = controller
        self.allow_close = False  # closing the window hides it to the tray unless exiting
        name = controller.ctx.settings.assistant_name
        self.setWindowTitle(f"{name} - {__app_name__}")
        self.resize(1600, 940)
        self.setMinimumSize(1360, 780)
        self._dark_title_bar_done = False

        # ================================================================ header
        self.logo = LogoRing()
        title = ShineTitle(" ".join(name.upper()), suffix="")  # a light sweeps across the name now and then
        subtitle = QLabel(f"UNIFIED COMMAND CENTER  ·  v{__version__}")
        subtitle.setObjectName("dashSub")
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(title)
        brand.addWidget(subtitle)
        # centre of the header: each item opens what it names (no decoration-only buttons)
        self.nav_buttons: dict[str, QPushButton] = {}
        nav = QHBoxLayout()
        nav.setSpacing(4)
        for key, text, color, tip in (("hud", "LIVE HUD", CYAN, "The live dashboard"),
                                      ("ai", "LOCAL AI", BLUE, "AI settings (models run on this PC)"),
                                      ("voice", "VOICE LINK", ORANGE, "Switch always-listening on / off"),
                                      ("local", "ALL ON THIS PC", DASH_DIM, "Privacy dashboard: what stays on this PC")):
            button = QPushButton(text)
            button.setObjectName("navItem")
            button.setToolTip(tip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(f"QPushButton#navItem {{ color:{color}; }}")
            self.nav_buttons[key] = button
            nav.addWidget(button)
        self.nav_buttons["hud"].setStyleSheet(f"QPushButton#navItem {{ color:{CYAN}; border:1px solid {COLOR['dash_edge']};"
                                              f" background: rgba(34, 211, 238, 0.13); }}")

        self.status_pill = QLabel("● STARTING")
        self.status_pill.setObjectName("pill")
        self.model_label = QLabel("Connecting to the AI model...")
        self.model_label.setObjectName("hudLabel")
        self.header_status = QLabel("")
        self.header_status.setObjectName("dashNav")
        self.header_status.setTextFormat(Qt.TextFormat.RichText)
        self.header_status.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.clock = QLabel()
        self.clock.setObjectName("dashClock")
        self.clock.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.date = QLabel()
        self.date.setObjectName("date")
        self.date.setAlignment(Qt.AlignmentFlag.AlignRight)
        when = QVBoxLayout()
        when.setSpacing(2)
        self.header_status.setVisible(False)      # its information is in the overview and the bottom cards
        clock_row = QHBoxLayout()
        clock_row.addStretch()
        clock_row.addWidget(self.date)
        clock_row.addSpacing(10)
        clock_row.addWidget(self.clock)
        when.addLayout(clock_row)

        self.menu_button = QPushButton("☰  MENU")
        self.menu_button.setToolTip("Settings, memory, automations, logs")
        self.menu = QMenu(self)
        self.hands_free_action = self.menu.addAction("Always listening (hands-free)")
        self.hands_free_action.setCheckable(True)
        self.hands_free_action.setChecked(controller.ctx.settings.hands_free)
        self.hands_free_action.toggled.connect(self._toggle_hands_free)
        self.mute_mic_action = self.menu.addAction("Mute microphone (Ctrl+M)")
        self.mute_mic_action.setCheckable(True)
        self.mute_mic_action.setToolTip("Nothing listens while this is on - not even the wake word")
        self.mute_mic_action.toggled.connect(lambda on: self.controller.set_mic_muted(on))
        self.menu.addSeparator()
        self.menu.addAction("Command center (Ctrl+Shift+C)", lambda: self.show_command_center())
        self.menu.addAction("Gesture control...", self.open_gestures)
        self.menu.addAction("Core view (Ctrl+Shift+H)", lambda: self.show_page(0))
        self.menu.addAction("Workspaces (every screen, Ctrl+K)...", self.open_workspaces)
        self.menu.addAction("Settings...", self.open_settings)
        self.memory_action = self.menu.addAction("Memory...", self.open_memory)
        self.menu.addAction("Privacy dashboard...", self.open_privacy)
        self.menu.addAction("Meeting / lecture notes (start / stop)", controller.toggle_notes)
        self.menu.addAction("Automations...", self.open_automations)
        self.menu.addAction("Mission Control (what Jarvis did)...", self.open_mission_control)
        self.menu.addAction("My day (to-dos, plan, history)...", self.open_my_day)
        self.menu.addAction("My life log (journal, habits, expenses, schedule)...", self.open_life_log)
        self.menu.addAction("Background tasks...", self.open_task_center)
        self.menu.addAction("Workflow studio...", self.open_workflow_studio)
        self.menu.addAction("Security center...", self.open_security_center)
        self.menu.addAction("Diagnostics & recovery...", self.open_diagnostics)
        self.notifications_action = self.menu.addAction("Notifications...", self.open_notifications)
        self.menu.addAction("Evolution Center...", self.open_evolution_center)
        self.menu.addAction("Plugins...", self.open_plugins)
        self.menu.addAction("Mobile devices...", self.open_mobile_devices)
        self.menu.addAction("What Jarvis sees (context)...", self.open_context_panel)
        self.menu.addAction("Knowledge...", self.open_knowledge)
        self.menu.addAction("AI and models...", self.open_models)
        self.menu.addAction("Device manager...", self.open_device_manager)
        self.menu.addAction("Phone screen...", self.open_phone_screen)
        self.menu.addAction("Emergency controls...", self.open_emergency)
        controller.notifications_changed.connect(
            lambda n: self.notifications_action.setText(f"Notifications ({n} new)..." if n else "Notifications..."))
        self.menu.addAction("Logs...", self.open_logs)
        self.menu.addAction("Enroll my voice...", controller.enroll_voice)
        self.menu.addAction("Teach me my wake word...", controller.teach_wake_word)
        self.menu.addAction("Train my voice (from my recordings)", controller.train_voice)
        self.menu.addAction("Make the Jarvis model...", self.make_jarvis_model)
        self.menu.addAction("Setup wizard...", self.open_setup_wizard)
        self.menu.addAction("Phone remote...", self.show_phone_remote)
        self.menu.addAction("Run self-test", self.run_self_test)
        self.menu.addAction("Speed report", controller.speed_report)
        self.menu.addAction("Health report", controller.health_report)
        self.menu.addAction("Export diagnostics...", self.export_diagnostics)
        self.menu.addAction("Check for updates", self.check_updates)
        self.menu.addSeparator()
        self.menu.addAction("New conversation", self.new_conversation)
        self.menu_button.setMenu(self.menu)

        header_box = QFrame()
        header_box.setObjectName("dashHeader")
        header = QGridLayout(header_box)
        header.setContentsMargins(12, 8, 12, 8)
        left, right = QHBoxLayout(), QHBoxLayout()
        left.addWidget(self.logo)
        left.addSpacing(10)
        left.addLayout(brand)
        left.addStretch()
        right.addStretch()
        right.addLayout(when)
        right.addSpacing(12)
        right.addWidget(self.menu_button, 0, Qt.AlignmentFlag.AlignVCenter)
        centre = QHBoxLayout()
        centre.addLayout(nav)
        centre.addSpacing(18)
        centre.addWidget(self.status_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addLayout(left, 0, 0)
        header.addLayout(centre, 0, 1, Qt.AlignmentFlag.AlignCenter)
        header.addLayout(right, 0, 2)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(2, 1)

        # ============================================================ SYSTEM panel
        system = HudPanel("System")
        system.setFixedWidth(330)

        def section(text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("sectionLabel")
            return label

        system.body.addWidget(section("VITALS"))
        self.cpu_gauge = SegmentBar("CPU", STATE_COLORS["IDLE"])
        self.gpu_gauge = SegmentBar("GPU", GOLD)
        self.ram_gauge = SegmentBar("Memory", COLOR["violet"])
        self.disk_gauge = SegmentBar("Disk", COLOR["cyan"])
        self.battery_gauge = SegmentBar("Battery", COLOR["ok"])
        for gauge in (self.cpu_gauge, self.gpu_gauge, self.ram_gauge, self.disk_gauge, self.battery_gauge):
            system.body.addWidget(gauge)
        system.body.addSpacing(4)
        system.body.addWidget(section("CPU · LAST 3 MINUTES"))
        self.cpu_history = Sparkline(60)
        system.body.addWidget(self.cpu_history)
        system.body.addWidget(section("NETWORK · ↓ CYAN  ↑ GOLD"))
        self.net_history = Sparkline(60, COLOR["accent"], second_color=GOLD, scale="auto", height=40)
        system.body.addWidget(self.net_history)
        self.rows: dict[str, HudStatRow] = {}
        for key, label in (("down", "Download"), ("up", "Upload")):
            row = HudStatRow(label)
            self.rows[key] = row
            system.body.addWidget(row)
        system.body.addSpacing(4)
        system.body.addWidget(section("DIAGNOSTICS"))
        for key, label in (("cpu", "Processor"), ("ram", "Memory"), ("battery", "Power"), ("disk", "Disk"),
                           ("gpu", "GPU"), ("uptime", "Uptime"), ("procs", "Processes"), ("host", "Host"),
                           ("os", "System"), ("voice", "Voice"), ("latency", "Timing")):
            row = HudStatRow(label)
            self.rows[key] = row
            system.body.addWidget(row)
        for hidden in ("battery", "disk"):  # shown by the meters above; kept for the read-outs
            self.rows[hidden].setVisible(False)
        system.body.addSpacing(4)
        system.body.addWidget(section("TOP PROCESSES"))
        self.process_rows = [HudStatRow("") for _ in range(5)]
        self.process_bars = [MiniBar(GOLD) for _ in range(5)]
        for row, bar in zip(self.process_rows, self.process_bars):
            row.name.setObjectName("hudValue")
            row.value.setObjectName("hudLabel")
            row.layout().insertWidget(2, bar, 0, Qt.AlignmentFlag.AlignVCenter)
            system.body.addWidget(row)
        system.body.addStretch()
        self._stats_ticks = 0
        self.cpu_label = self.rows["cpu"].value  # kept names: tests and older code use these
        self.ram_label = self.rows["ram"].value
        self.battery_label = self.rows["battery"].value
        self.rows["host"].set(socket.gethostname()[:22])
        self.rows["os"].set(f"{platform.system()} {platform.release()}"[:22])
        stt = getattr(controller, "stt", None)
        self.rows["voice"].set("loading…" if not getattr(stt, "loaded", False)
                               else getattr(stt, "description", "ready").replace(" on ", " · "))
        self.rows["latency"].set("--")
        import shutil as _shutil

        self._gpu_available = _shutil.which("nvidia-smi") is not None
        self._gpu_text = "--"
        self._gpu_util: float | None = None
        self._gpu_thread = None
        self.rows["gpu"].setVisible(self._gpu_available)
        self.gpu_gauge.setVisible(self._gpu_available)
        system_scroll = QScrollArea()
        system_scroll.setWidget(system)
        system_scroll.setWidgetResizable(True)
        system_scroll.setFixedWidth(346)
        system_scroll.setFrameShape(QFrame.Shape.NoFrame)
        system_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        system_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")

        # ================================================================= core
        from app.ui.core3d.widget import create_core

        # the 3D core (OpenGL) is the whole window background, with the dashboard on top of it; the core itself is drawn in
        # `core_slot`. Without OpenGL the flat core sits in the slot's place and the painter backdrop is the background.
        self.orb = create_core(controller.ctx.settings, backdrop=True)
        self._gl_backdrop = getattr(self.orb, "backdrop_mode", False)
        self.core_slot = QWidget() if self._gl_backdrop else self.orb
        self.core_slot.setMinimumSize(300, 300)
        self.state_label = QLabel(STATE_LABELS["IDLE"].upper())
        self.state_label.setObjectName("state")
        self.hint_label = QLabel("PRESS TALK OR TYPE BELOW")
        self.hint_label.setObjectName("hint")
        self.hint_label.setWordWrap(True)

        self.mic_indicator = Indicator("MICROPHONE ACTIVE", COLOR["ok"])
        self.speaking_indicator = Indicator("SPEAKING", "#60a5fa")
        self.browser_indicator = Indicator("BROWSER ACTIVE", COLOR["warn"])
        self.screen_indicator = Indicator("SCREEN CAPTURE ACTIVE", COLOR["danger"])
        self.hands_free_indicator = Indicator("HANDS-FREE", COLOR["violet"])
        self.offline_indicator = Indicator("NO INTERNET", "#94a3b8")
        self.camera_indicator = Indicator("CAMERA ACTIVE", COLOR["danger"])
        self.notes_indicator = Indicator("TAKING NOTES", COLOR["gold"])
        self.muted_indicator = Indicator("MIC MUTED", COLOR["danger"])        # nothing listens (not even the wake word)
        self.transcribing_indicator = Indicator("TRANSCRIBING", COLOR["cyan"])  # what you said is being turned into text
        self._screen_off_timer = QTimer(self)  # keep it up a few seconds so a quick capture is still noticed
        self._screen_off_timer.setSingleShot(True)
        self._screen_off_timer.timeout.connect(lambda: self.screen_indicator.setVisible(False))
        indicators = QHBoxLayout()
        indicators.setSpacing(6)
        indicators.addStretch()
        for badge in (self.offline_indicator, self.muted_indicator, self.hands_free_indicator, self.mic_indicator,
                      self.transcribing_indicator, self.speaking_indicator,
                      self.browser_indicator, self.screen_indicator, self.camera_indicator, self.notes_indicator):
            indicators.addWidget(badge)
        indicators.addStretch()

        # the status card and quick actions sit left of the core (the core itself is unchanged)
        self.online_label = QLabel(f"{name.upper()} IS ONLINE")
        self.online_label.setObjectName("dashOnline")
        self.online_label.setWordWrap(True)
        self.level_bars = LevelBars()
        status_card = DashPanel("", accent=CYAN, margins=(12, 10, 12, 10))
        status_card.body.addWidget(self.online_label)
        status_card.body.addWidget(self.state_label)
        status_card.body.addWidget(self.hint_label)
        status_card.body.addSpacing(4)
        status_card.body.addWidget(self.level_bars)

        # quick actions: each one just sends the spoken command, so it goes through the normal checks
        self.quick_actions: dict[str, QPushButton] = {}
        actions = QVBoxLayout()
        actions.setSpacing(6)
        for label, command in (("⚙  SYSTEM SCAN", "system status"), ("☀  MORNING BRIEFING", "good morning"),
                               ("⛅  WEATHER", "what's the weather"), ("⏯  PLAY / PAUSE", "pause"),
                               ("⏭  NEXT SONG", "next song"), ("🔒  LOCK PC", "lock my pc")):
            chip = QPushButton(label)
            chip.setObjectName("dashAction")
            chip.setToolTip(f"Same as saying \"{command}\"")
            chip.clicked.connect(lambda _=False, text=command: controller.send_text(text))
            self.quick_actions[command] = chip
            actions.addWidget(chip)
        self.meters = VerticalMeters()
        self.meters_caption = QLabel("")          # kept for older code; the frequency card shows the values
        self.meters_caption.setVisible(False)
        self.freq_card = FrequencyCard()
        freq_panel = DashPanel("", accent=PURPLE, margins=(8, 6, 8, 6))
        freq_panel.body.addWidget(self.freq_card)
        side = QVBoxLayout()
        side.setSpacing(8)
        side.addWidget(status_card)
        side.addWidget(self.meters)
        side.addSpacing(6)
        side.addLayout(actions)
        side.addStretch()
        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(200)

        core_column = QVBoxLayout()
        core_column.setSpacing(4)
        core_column.addWidget(self.core_slot, 1)
        self.spectrum = SpectrumStrip()
        core_column.addWidget(self.spectrum)
        self.engine_line = QLabel("")
        self.engine_line.setObjectName("engineLine")
        self.engine_line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.engine_line.setTextFormat(Qt.TextFormat.RichText)
        core_column.addWidget(self.engine_line)
        core_column.addLayout(indicators)
        core_panel = DashPanel("", accent=CYAN, margins=(10, 10, 10, 10))
        core_row = QHBoxLayout()
        core_row.setSpacing(8)
        core_row.addWidget(side_widget)
        core_row.addLayout(core_column, 1)
        core_panel.body.addLayout(core_row, 1)
        core_panel.setMinimumWidth(560)

        # =========================================================== left column
        overview = DashPanel("System overview", accent=CYAN)
        self.overview: dict[str, StatusRow] = {}
        for key, label in (("ai", "AI core"), ("model", "Model"), ("speech", "Speech recognition"),
                           ("voice", "Voice output"), ("internet", "Internet"), ("actions", "Risky actions")):
            row = StatusRow(label)
            self.overview[key] = row
            overview.body.addWidget(row)
        modules = DashPanel("Active modules", accent=CYAN)
        self.modules: dict[str, ModuleRow] = {}
        for key, label in (("voice", "Voice / interact"), ("hands_free", "Hands-free"), ("screen", "Screen agent"),
                           ("browser", "Browser"), ("home", "Smart home"), ("mcp", "MCP servers"),
                           ("remote", "Phone remote"), ("notes", "Meeting notes")):
            row = ModuleRow(label, switch=key == "hands_free")    # hands-free is a real switch; the rest show state
            self.modules[key] = row
            modules.body.addWidget(row)
        self.modules["hands_free"].toggled.connect(lambda: self._toggle_hands_free(self.controller.hands_free is None))
        events = DashPanel("Live event log", accent=CYAN)
        self.events = EventLog()
        events.body.addWidget(self.events, 1)
        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(overview)
        left.addWidget(modules)
        left.addWidget(events, 1)
        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setFixedWidth(250)

        # ========================================================= right columns
        link = DashPanel("Link monitor", accent=CYAN)
        self.donut_cpu, self.donut_ram = DonutGauge(f"{os.cpu_count() or '?'} cores", BLUE), DonutGauge("Memory", PURPLE)
        donuts = QHBoxLayout()
        donuts.addWidget(self.donut_cpu)
        donuts.addWidget(self.donut_ram)
        link.body.addLayout(donuts)
        self.link: dict[str, StatusRow] = {}
        for key, label in (("network", "Network"), ("down", "Download"), ("up", "Upload"), ("uptime", "Uptime"),
                           ("host", "Host")):
            row = StatusRow(label)
            self.link[key] = row
            row.setVisible(False)            # the picture shows only the gauges; the values are on the SYSTEM page
        self.link["host"].set(socket.gethostname()[:18], "info")
        performance = DashPanel("System performance", accent=CYAN)
        self.perf = {"cpu": ThinBar("CPU", BLUE), "ram": ThinBar("Memory", PURPLE), "gpu": ThinBar("GPU", GOLD),
                     "disk": ThinBar("Disk", CYAN)}
        for bar in self.perf.values():
            performance.body.addWidget(bar)
        self.perf["gpu"].setVisible(self._gpu_available)
        power = DashPanel("Power core", accent=CYAN)
        self.power = CoreGauge()
        power.body.addWidget(self.power, 1)
        right1 = QVBoxLayout()
        right1.setSpacing(10)
        right1.addWidget(link)
        right1.addWidget(performance)
        right1.addWidget(power, 1)
        right1_widget = QWidget()
        right1_widget.setLayout(right1)
        right1_widget.setFixedWidth(240)

        timing = DashPanel("Response // timing", accent=CYAN)
        self.timing_big = QLabel("-- s")
        self.timing_big.setObjectName("dashBig")
        timing.body.addWidget(self.timing_big)
        self.timing_rows: dict[str, StatusRow] = {}
        for key, label in (("heard", "Hearing"), ("ai", "Thinking"), ("voice", "Speaking")):
            row = StatusRow(label)
            self.timing_rows[key] = row
            timing.body.addWidget(row)
        approval = DashPanel("Approval queue", accent=CYAN)
        self.approval_label = QLabel("No actions waiting.")
        self.approval_label.setObjectName("dashMuted")
        self.approval_label.setWordWrap(True)
        approval.body.addWidget(self.approval_label)
        comms = DashPanel("", accent=CYAN)
        comms.frameless = True                  # no title, border or background: the chat floats on the backdrop
        self.chat = ChatView(name)
        comms.body.addWidget(self.chat, 1)
        timing.setVisible(False)             # timing and approvals: the bottom live box / the confirmation dialog
        approval.setVisible(False)
        self._hidden_panels = (timing, approval)
        self.panels = {"System overview": overview, "Active modules": modules, "Live event log": events,
                       "Link monitor": link, "System performance": performance, "Power core": power,
                       "Conversation": comms}
        address = controller.ctx.settings.user_address.strip()
        self.comms_input = QLineEdit()
        self.comms_input.setObjectName("dashInput")
        self.comms_input.setAccessibleName("Message to Jarvis (conversation panel)")
        self.comms_input.setPlaceholderText(f"How can I help you{', ' + address if address else ''}?")
        self.comms_input.returnPressed.connect(self._send_comms)
        comms_mic = QPushButton("🎙")
        comms_mic.setObjectName("micSmall")
        comms_mic.setAccessibleName("Talk")
        comms_mic.setToolTip("Push to talk")
        comms_mic.clicked.connect(controller.toggle_listening)
        comms_send = QPushButton("SEND")
        comms_send.setObjectName("primary")
        comms_send.clicked.connect(self._send_comms)
        comms_row = QHBoxLayout()
        comms_row.setSpacing(6)
        comms_row.addWidget(self.comms_input, 1)
        comms_row.addWidget(comms_mic)
        comms_row.addWidget(comms_send)
        comms_stop = QPushButton("■  STOP")
        comms_stop.setToolTip("Stop speaking / cancel (Esc)")
        comms_stop.clicked.connect(controller.cancel)
        self.live_box_side = QLabel("")
        self.live_box_side.setObjectName("liveBox")
        stop_row = QHBoxLayout()
        stop_row.setSpacing(6)
        stop_row.addWidget(comms_stop)
        stop_row.addWidget(self.live_box_side, 1)
        right2 = QVBoxLayout()
        right2.setSpacing(8)
        right2.addWidget(comms, 1)
        right2.addLayout(comms_row)
        right2.addLayout(stop_row)
        right2_widget = QWidget()
        right2_widget.setLayout(right2)
        right2_widget.setMinimumWidth(330)
        right2_widget.setMaximumWidth(420)

        dashboard = QWidget()
        grid = QHBoxLayout(dashboard)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        # left and right columns have the same width, so the core sits exactly in the middle of the window
        side_column = QWidget()
        side_layout = QVBoxLayout(side_column)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.addWidget(freq_panel)
        side_layout.addStretch()
        side_column.setFixedWidth(380)
        right2_widget.setMinimumWidth(380)
        right2_widget.setMaximumWidth(380)
        grid.addWidget(left_widget)
        grid.addWidget(side_column)
        grid.addWidget(core_panel, 1)
        grid.addWidget(right1_widget)
        grid.addWidget(right2_widget)

        # the detailed SYSTEM page: the full vitals panel
        system_scroll.setFixedWidth(560)
        system.setFixedWidth(540)
        system_page = QWidget()
        system_row = QHBoxLayout(system_page)
        system_row.setContentsMargins(0, 0, 0, 0)
        system_row.addStretch()
        system_row.addWidget(system_scroll)
        system_row.addStretch()

        self.pages = QStackedWidget()
        self.pages.addWidget(dashboard)
        self.pages.addWidget(system_page)

        # ================================================================ tabs
        self.tabs: list[TabButton] = []
        tab_row = QHBoxLayout()
        tab_row.setSpacing(6)
        for text, accent, action in (
            ("Core live", ORANGE, lambda: self.show_page(0)),
            ("System", BLUE, lambda: self.show_page(1)),
            ("Memory", PURPLE, self.open_memory),
            ("Automations", GREEN, self.open_automations),
            ("Notes", GOLD, controller.toggle_notes),
            ("Devices", CYAN, lambda: controller.send_text("smart home status")),
            ("Phone", BLUE, self.show_phone_remote),
            ("Privacy", PURPLE, self.open_privacy),
            ("Logs", CYAN, self.open_logs),
            ("Settings", GOLD, self.open_settings),
        ):
            tab = TabButton(text, accent)
            tab.clicked.connect(action)
            self.tabs.append(tab)
        self.page_indicator = TabButton("Core live", ORANGE)
        self.page_indicator.setToolTip("The page you are on")
        self.page_indicator.clicked.connect(lambda: self.show_page(0))
        self.page_indicator.set_current(True)
        self.page_indicator.setFixedWidth(250)
        tab_row.addWidget(self.page_indicator)
        tab_row.addStretch(1)
        for tab in self.tabs[:5]:
            tab.setFixedWidth(150)
            tab_row.addWidget(tab)
        tab_row.addStretch(2)
        # the tab row and the quick-action buttons are hidden, so everything they did is in the MENU (same handlers)
        more = QMenu(self)
        for tab in self.tabs:                # Core live, System, Memory, Automations, Notes, Devices, Phone, Privacy, ...
            more.addAction(tab.text().title(), tab.click)
        first = self.menu.actions()[0]
        self.menu.insertMenu(first, more).setText("Pages")
        self.menu.insertAction(first, self.menu.addAction("Show all windows", lambda: self.open_overview("windows")))
        self.menu.insertAction(first, self.menu.addAction("Show browser tabs", lambda: self.open_overview("tabs")))
        quick = QMenu(self)
        for chip in self.quick_actions.values():
            quick.addAction(chip.text().split("  ", 1)[-1].title(), chip.click)
        self.menu.insertMenu(first, quick).setText("Quick actions")
        self.menu.insertSeparator(first)
        self.tabs[0].set_current(True)

        # ======================================================= bottom cards
        self.cards = {"ai": StatusCard("AI core", CYAN, "CHECK"), "voice": StatusCard("Voice engine", GREEN, "SPEED"),
                      "memory": StatusCard("Memory", PURPLE, "OPEN"),
                      "automations": StatusCard("Automations", GOLD, "OPEN"),
                      "security": StatusCard("Security", ORANGE, "OPEN")}
        self.cards["ai"].clicked.connect(self.run_self_test)
        self.cards["voice"].clicked.connect(controller.speed_report)
        self.cards["memory"].clicked.connect(self.open_memory)
        self.cards["automations"].clicked.connect(self.open_automations)
        self.cards["security"].clicked.connect(self.open_privacy)
        cards = QHBoxLayout()
        cards.setSpacing(10)
        for card in self.cards.values():
            cards.addWidget(card)

        # ========================================================= input bar
        address = controller.ctx.settings.user_address.strip()
        self.input = QLineEdit()
        self.input.setObjectName("dashInput")
        self.input.setAccessibleName("Message to Jarvis")
        self.input.setPlaceholderText("")        # the hint text was removed at your request
        self.input.returnPressed.connect(self._send)
        self.send_button = QPushButton("SEND ➤")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self._send)
        self.mic_button = QPushButton("🎤  TALK")
        self.mic_button.setObjectName("mic")
        self.mic_button.setToolTip("Push to talk (Ctrl+Shift+J)")
        self.mic_button.clicked.connect(controller.toggle_listening)
        self.stop_button = QPushButton("■  STOP")
        self.stop_button.setToolTip("Stop speaking / cancel (Esc)")
        self.stop_button.clicked.connect(controller.cancel)
        self.live_voice_button = QPushButton("LIVE VOICE: OFF")
        self.live_voice_button.setObjectName("liveVoice")
        self.live_voice_button.setToolTip("Always listening: say the wake phrase any time (click to switch)")
        self.live_voice_button.clicked.connect(lambda: self._toggle_hands_free(self.controller.hands_free is None))
        self.live_box = QLabel("")
        self.live_box.setObjectName("liveBox")
        self.live_box.setFixedWidth(230)
        self._last_action = "--"
        self.command_center = None            # built the first time it is opened (Ctrl+Shift+C)
        self._bottom_bar = [self.live_voice_button, self.input, self.mic_button, self.send_button, self.stop_button]
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        input_row.addWidget(self.live_voice_button)
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.mic_button)
        input_row.addWidget(self.send_button)
        input_row.addWidget(self.stop_button)
        input_row.addWidget(self.live_box)

        root = QVBoxLayout()
        root.setContentsMargins(14, 10, 14, 6)
        root.setSpacing(10)
        root.addWidget(header_box)
        root.addLayout(tab_row)
        root.addWidget(self.pages, 1)
        root.addLayout(cards)
        root.addLayout(input_row)
        central = self.orb if self._gl_backdrop else HexBackground()
        central.setLayout(root)
        self.setCentralWidget(central)
        self.backdrop = central
        if self._gl_backdrop:
            self.orb.slot = self.core_slot
        central.start_motion()  # scan beam and hex pulses (ANIMATIONS=false turns it off)
        self.setStatusBar(QStatusBar())
        self.statusBar().setVisible(False)       # the footer line was removed at your request

        self.nav_buttons["hud"].clicked.connect(lambda: self.show_page(0))
        self.nav_buttons["ai"].clicked.connect(self.open_settings)
        self.nav_buttons["voice"].clicked.connect(lambda: self._toggle_hands_free(self.controller.hands_free is None))
        self.nav_buttons["local"].clicked.connect(self.open_privacy)

        # removed from view at your request (the objects stay: other code still updates them, nothing breaks):
        # header subtitle + centre items, the whole left column, the status card, the side STOP/live box,
        # the bottom status cards and the bottom live box
        for widget in (subtitle, *self.nav_buttons.values(), left_widget, self.page_indicator, status_card,
                       comms_stop, self.live_box_side, *self.cards.values(), self.live_box,
                       *self.tabs, side_widget, right1_widget, self.engine_line,
                       self.comms_input, comms_mic, comms_send):
            widget.setVisible(False)
        self.orb.hidden_readouts = {"cpu", "gpu", "ram"}       # only the battery read-out stays around the core
        core_panel.frameless = True                             # no border around the core
        painter_core = getattr(self.orb, "model", self.orb)     # the object that owns the wave history
        painter_core.external_holo = True
        self.orb.external_holo = True

        def holo():
            if self._gl_backdrop:
                return None                     # the GPU draws the waves itself
            if not self.orb.isVisible() or self.pages.currentIndex() != 0:
                return None
            centre = self.orb.mapTo(central, self.orb.rect().center())
            radius = min(self.orb.width(), self.orb.height()) * 0.41
            tint = (QColor.fromRgbF(*self.orb.dyn.tint) if hasattr(self.orb, "dyn")
                    else QColor(STATE_COLORS.get(self.orb.state, STATE_COLORS["IDLE"])))
            return QPointF(centre), radius, tint, painter_core

        central.holo_source = holo
        if hasattr(self.orb, "frame_rendered") and not self._gl_backdrop:
            self.orb.frame_rendered.connect(central.holo_frame)    # the waves move in step with the core
        if hasattr(self.orb, "model"):
            self.orb.model.hidden_readouts = self.orb.hidden_readouts
        self._apply_hud_rings()
        from app.ui.dashboard_fx import wire_fx

        wire_fx(self, controller)          # data pulses, interface sounds, real speech level, hidden panels

        # ---- window-local shortcuts (Esc never grabbed globally)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=controller.cancel)
        QShortcut(QKeySequence(Qt.Key.Key_F11), self, activated=self.toggle_full_screen)
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.open_workspaces)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self.show_command_center)
        QShortcut(QKeySequence("Ctrl+Shift+H"), self, activated=lambda: self.show_page(0))
        QShortcut(QKeySequence("Ctrl+M"), self, activated=lambda: self.controller.set_mic_muted(
            not self.controller.mic_muted))

        # ---- controller signals
        c = controller
        c.state_changed.connect(self._on_state)
        c.step_aside_requested.connect(self.step_aside)
        c.user_message.connect(lambda text: self.chat.add_message("user", text, c.spoken_wave()))
        c.assistant_started.connect(self.chat.begin_stream)
        c.assistant_delta.connect(self.chat.append_stream)
        c.assistant_finished.connect(self._on_finished)
        c.tool_event.connect(lambda text: self.chat.add_message("tool", text))
        c.notice.connect(lambda text: self.chat.add_message("notice", text))
        c.status_changed.connect(self._on_status)
        c.confirmation_requested.connect(self._on_confirmation)
        c.microphone_active.connect(self._on_mic)
        c.speaking_changed.connect(self.speaking_indicator.setVisible)
        c.browser_active.connect(self.browser_indicator.setVisible)
        c.screen_capture_active.connect(self._on_screen_capture)
        c.hands_free_state.connect(self._on_hands_free)
        c.reminder.connect(lambda title, message: self.chat.add_message("notice", f"⏰ {title}: {message}"))
        c.level_changed.connect(self.orb.set_level)
        c.display_settings_changed.connect(self._apply_core_settings)
        c.voice_engine_changed.connect(self._on_voice_engine)
        c.timing.connect(self._on_timing)
        c.notes_line.connect(self._on_partial)
        c.camera_active.connect(self.camera_indicator.setVisible)
        c.notes_active.connect(self.notes_indicator.setVisible)
        c.mic_muted_changed.connect(self._on_mic_muted)
        c.transcribing.connect(self.transcribing_indicator.setVisible)
        c._update_found.connect(self._on_update_found)
        c.partial_transcript.connect(self._on_partial)
        c.confirmation_answered.connect(self._on_confirmation_answered)
        # ---- the dashboard's event bus and live read-outs
        c.tool_event.connect(self._on_tool_event)
        c.notice.connect(lambda text: self.events.add("notice", text))
        c.reminder.connect(lambda title, message: self.events.add("reminder", f"{title}: {message}"))
        c.user_message.connect(lambda text: self.events.add("user", f"heard: {text}"))
        c.level_changed.connect(lambda dbfs: self._on_level(max(0.0, min(1.0, (dbfs + 60.0) / 50.0))))
        c.notes_active.connect(lambda _on: self._refresh_dashboard())
        c.jarvis_action.connect(self._on_jarvis_action)
        c.mobile_pending.connect(self._mobile_pending)
        self._confirm_dialog = None
        self.mini_orb = None
        if controller.ctx.settings.mini_orb:
            from app.ui.mini_orb import MiniOrb

            self.mini_orb = MiniOrb(controller.ctx.settings.mini_orb_size)
            c.state_changed.connect(self.mini_orb.set_state)
            c.level_changed.connect(self.mini_orb.set_level)
            self.mini_orb.open_requested.connect(self.bring_back)
            self.mini_orb.hide_requested.connect(self.mini_orb.hide)

        # ---- timers (paused while hidden)
        self._ai_ok: bool | None = None
        self._last_problem: str | None = None
        self._net_last: tuple[float, int, int] | None = None
        # full screen like its own OS (START_FULLSCREEN): also when Jarvis started in the tray and is opened later
        self.wants_full_screen = bool(getattr(controller.ctx.settings, "start_fullscreen", True))
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(int(monitor_interval * 1000))
        self._stats_timer.timeout.connect(self._tick_stats)
        self.stats_ready.connect(self._apply_stats)
        self._state_watchdog = QTimer(self)  # a busy state that isn't true any more ends (never stuck)
        self._state_watchdog.setInterval(5000)
        self._state_watchdog.timeout.connect(lambda: getattr(self.controller, "check_state", lambda: None)())
        self._state_watchdog.start()
        psutil.cpu_percent(None)  # prime the counter
        self._tick_clock()
        self._tick_stats()
        self._set_pill()

    # ------------------------------------------------------------------ slots

    def _send(self) -> None:
        text = self.input.text()
        if self.controller.send_text(text):
            self.input.clear()

    def _on_state(self, state: str) -> None:
        self.orb.set_state(state)
        self.backdrop.set_state(state)
        DashPanel.set_activity(state)  # thinking / speaking: a light runs along every panel title
        self.state_label.setText(STATE_LABELS.get(state, state).upper())
        self.state_label.setStyleSheet(f"color: {STATE_COLORS.get(state, '#dbe4f3')};")
        self.mic_button.setText("■  END" if state == "LISTENING" else "🎤  TALK")
        if state != getattr(self, "_last_state", None):
            self._last_state = state
            if state != "IDLE":
                self.events.add("error" if state == "ERROR" else "state", STATE_LABELS.get(state, state).upper())
        self._update_live_box()

    # ---------------------------------------------------------------- full screen and the entrance
    wants_full_screen = False

    def show_start(self, full_screen: bool, fade: bool = True) -> None:
        """Appear after the intro: full screen (START_FULLSCREEN), fading in while the panels draw themselves."""
        self.wants_full_screen = full_screen
        self._started = True
        fade = fade and self._can_fade()
        if fade:
            self.setWindowOpacity(0.0)
        if full_screen:
            self.showFullScreen()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self.bring_to_front()
        DashPanel.replay_boot()
        if fade:
            from PySide6.QtCore import QEasingCurve, QPropertyAnimation

            self._fade = QPropertyAnimation(self, b"windowOpacity", self)
            self._fade.setDuration(700)
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._fade.start()

    def step_aside(self, done=None) -> None:
        """Fade out into the mini orb, so the app Jarvis just opened (or you're typing into) is what you see.
        The orb keeps showing Jarvis's state; click it or say "show yourself" to come back."""
        def finish() -> None:
            if done is not None:
                done.set()

        if self.isHidden() or self.isMinimized() or not getattr(self, "_started", True):
            finish()
            return
        from app.ui import motion

        if not motion.ENABLED or not self._can_fade():
            self.showMinimized()
            self._front_target()
            finish()
            return
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation

        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(300)
        fade.setStartValue(self.windowOpacity())
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InCubic)

        def gone() -> None:
            self.showMinimized()
            self.setWindowOpacity(1.0)  # ready for the way back
            self._front_target()
            finish()

        fade.finished.connect(gone)
        self._step_fade = fade
        fade.start()

    @staticmethod
    def _front_target() -> None:
        """The app you're working with (the one just opened) is the one in front once Jarvis stepped aside."""
        try:
            from app.tools import winapi

            target = winapi.target_window()
            if target is not None:
                winapi.force_foreground(target.hwnd)
        except Exception:
            pass

    def _can_fade(self) -> bool:
        """Whole-window opacity fades turn the window into a Windows layered window; with the OpenGL backdrop that froze
        the core and then crashed some GPU drivers (access violation right after the window appeared). The panels
        still draw themselves in (DashPanel.replay_boot), only the window-level fade is skipped."""
        return not getattr(self, "_gl_backdrop", False)

    def bring_to_front(self) -> None:
        """In front of Chrome and everything else, even when another app has the focus (Windows allows
        that only to the app in front; see winapi.force_foreground). ALWAYS_ON_TOP keeps it there."""
        from app.tools import winapi

        try:
            hwnd = int(self.winId())
            winapi.force_foreground(hwnd)
            if getattr(self.controller.ctx.settings, "always_on_top", False):
                winapi.set_topmost(hwnd, True)
        except Exception:
            pass

    def set_always_on_top(self, on: bool) -> None:
        from app.tools import winapi

        try:
            winapi.set_topmost(int(self.winId()), on)
        except Exception:
            pass
        apply = getattr(self.controller.ctx, "settings", None)
        if apply is not None:
            self.controller.ctx.settings = apply.model_copy(update={"always_on_top": on})

    def bring_back(self) -> None:
        """From the tray / a hotkey: back as it was (full screen if that's how Jarvis runs)."""
        if not getattr(self, "_started", True):
            intro = getattr(self, "_intro", None)  # the intro is still playing: finish it, it brings us in
            try:
                if intro is not None and intro.isVisible():
                    intro.skip()
            except RuntimeError:  # it already closed itself
                pass
            return
        coming_back = self.isMinimized() or self.isHidden()
        from app.ui import motion

        fade = coming_back and motion.ENABLED and self._can_fade()
        if fade:
            self.setWindowOpacity(0.0)
        if self.wants_full_screen:
            self.showFullScreen()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()
        self.bring_to_front()
        if coming_back and motion.ENABLED:
            DashPanel.replay_boot()
        if fade:  # back with a fade, the panels drawing themselves in again
            from PySide6.QtCore import QEasingCurve, QPropertyAnimation

            self._back_fade = QPropertyAnimation(self, b"windowOpacity", self)
            self._back_fade.setDuration(450)
            self._back_fade.setStartValue(0.0)
            self._back_fade.setEndValue(1.0)
            self._back_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._back_fade.start()

    def toggle_full_screen(self) -> None:
        self.set_full_screen(not self.isFullScreen())

    def set_full_screen(self, on: bool) -> None:
        self.wants_full_screen = on
        if on:
            self.showFullScreen()
        else:
            self.showNormal()

    SCREEN_INDICATOR_MIN_MS = 3000

    def _on_screen_capture(self, active: bool) -> None:
        if active:
            self._screen_off_timer.stop()
            self.screen_indicator.setVisible(True)
        else:
            self._screen_off_timer.start(self.SCREEN_INDICATOR_MIN_MS)

    def _toggle_hands_free(self, enabled: bool) -> None:
        if enabled != (self.controller.hands_free is not None):
            self.controller.set_hands_free(enabled)

    def _on_hands_free(self, state: str) -> None:
        wake = self.controller.ctx.settings.wake_word.upper()
        labels = {"starting": "HANDS-FREE...", "waiting": "HANDS-FREE", "command": "HANDS-FREE",
                  "follow_up": "IN CONVERSATION", "busy": "HANDS-FREE"}
        self.hands_free_indicator.setText(labels.get(state, "HANDS-FREE"))
        self.hands_free_indicator.setVisible(state in labels)
        running = self.controller.hands_free is not None and state != "error"
        self.hands_free_action.blockSignals(True)
        self.hands_free_action.setChecked(self.controller.hands_free is not None)
        self.hands_free_action.blockSignals(False)
        self.mic_button.setToolTip(f"Hands-free is on: just say \"{wake.title()}\". Click to skip the wake phrase."
                                   if running else "Talk (push-to-talk)")
        name = self.controller.ctx.settings.assistant_name.upper()
        hints = {"waiting": f"SAY \"{name}\" ANY TIME", "follow_up": "LISTENING · SAY \"GOODBYE\" TO END",
                 "command": "LISTENING...", "starting": "STARTING HANDS-FREE..."}
        self.hint_label.setText(hints.get(state, "PRESS TALK OR TYPE BELOW") if running or state == "starting"
                                else "PRESS TALK OR TYPE BELOW")
        on = self.controller.hands_free is not None and state != "stopped"
        self.live_voice_button.setText("LIVE VOICE: ON" if on else "LIVE VOICE: OFF")
        self.live_voice_button.setProperty("on", "true" if on else "false")
        self.live_voice_button.style().unpolish(self.live_voice_button)
        self.live_voice_button.style().polish(self.live_voice_button)
        self._refresh_dashboard()

    def _on_mic_muted(self, muted: bool) -> None:
        self.muted_indicator.setVisible(muted)
        self.mute_mic_action.blockSignals(True)
        self.mute_mic_action.setChecked(muted)
        self.mute_mic_action.blockSignals(False)
        if muted:
            self.transcribing_indicator.setVisible(False)
            self.hint_label.setText("MICROPHONE MUTED · CTRL+M TO UNMUTE")
        self.mic_button.setToolTip("The microphone is muted (Ctrl+M to unmute)" if muted else "Talk (push-to-talk)")

    def _on_mic(self, active: bool) -> None:
        self.mic_indicator.setVisible(active)
        self.mic_button.setProperty("active", "true" if active else "false")
        self.mic_button.style().unpolish(self.mic_button)
        self.mic_button.style().polish(self.mic_button)

    def _on_finished(self, reply) -> None:
        self.chat.finish_reply(reply.text)  # shown only if nothing was streamed
        if getattr(reply, "elapsed_ms", None) is not None:
            how = "direct" if reply.model == "direct" else "AI"
            if "🧠" not in self.rows["latency"].text():
                self.rows["latency"].set(f"{reply.elapsed_ms / 1000:.1f} s · {how}")

    def _on_voice_engine(self, description: str) -> None:
        """"large-v3-turbo on GPU" -> "large-v3-turbo · GPU"."""
        self.rows["voice"].set(description.replace(" on ", " · "))
        self._refresh_dashboard()

    def _on_status(self, status: AIStatus) -> None:
        self._ai_ok = bool(status.reachable and status.selected is not None)
        self._set_pill()
        problem = None
        if not status.reachable:
            self.model_label.setText("WAITING FOR THE AI · RETRYING")
            problem = (status.message or "The AI model is not reachable.") + " I'll keep trying."
            # (the red PROBLEM state is set - and cleared again - by the controller, which knows the real state;
            # painting it here directly left the window red after the AI had connected)
        elif status.selected is None:
            self.model_label.setText("NO MODEL")
            problem = status.message or "No AI model is installed."
        if problem is not None:
            self._model_name = ""
            self._refresh_dashboard()
            if problem != self._last_problem:  # retries repeat the same problem: show it once
                self.chat.add_message("error", problem)
                self.events.add("error", problem)
            self._last_problem = problem
            return
        self._last_problem = None
        self.model_label.setText(f"MODEL · {status.selected.name.upper()}")
        self._model_name = status.selected.name
        self._refresh_dashboard()

    def _on_confirmation(self, pending: PendingConfirmation) -> None:
        if self.isHidden() or self.isMinimized():
            self.showFullScreen() if self.wants_full_screen else self.showNormal()
        self.raise_()
        self.activateWindow()
        dialog = ConfirmDialog(pending.request, pending.heard, parent=self)
        self._confirm_dialog = (pending, dialog)
        self.approval_label.setText(f"WAITING FOR YOU: {pending.request.summary}")
        self.approval_label.setStyleSheet(f"color:{ORANGE};")
        self.events.add("reminder", f"approval needed: {pending.request.summary}")
        approved = dialog.exec() == ConfirmDialog.DialogCode.Accepted
        self._confirm_dialog = None
        if pending.future.done():  # answered by voice ("yes" / "হ্যাঁ")
            approved = bool(pending.future.result())
        self.controller.resolve_confirmation(pending, approved)
        self.chat.add_message("notice", "Confirmed." if approved else "Cancelled.")
        self.events.add("ok" if approved else "state", "approved" if approved else "declined")
        self.approval_label.setStyleSheet("")
        self._refresh_approvals()

    def _on_confirmation_answered(self, pending, approved: bool) -> None:
        current = self._confirm_dialog
        if current is not None and current[0] is pending:
            current[1].done(ConfirmDialog.DialogCode.Accepted if approved else ConfirmDialog.DialogCode.Rejected)

    def _on_timing(self, timing: dict) -> None:
        """HEARD 0.3 s · AI 1.2 s · VOICE 0.5 s: where the time of the last request went."""
        parts = []
        for key, label in (("heard", "🎤"), ("ai", "🧠"), ("voice", "🔊")):
            value = timing.get(key)
            if value is not None:
                parts.append(f"{label}{value:.1f}")
        total = sum(v for v in (timing.get("heard"), timing.get("ai"), timing.get("voice")) if v is not None)
        self.timing_big.setText(f"{total:.1f} s")
        self.timing_big.setStyleSheet(f"color:{GREEN if total < 2.5 else ORANGE if total < 5 else COLOR['danger']};")
        for key, row in self.timing_rows.items():
            value = timing.get(key)
            row.set("--" if value is None else f"{value:.1f} s", "off" if value is None else "info")
        if parts:
            self.rows["latency"].set(" · ".join(parts) + " s")
            self.rows["latency"].setToolTip("Heard = speech recognition, AI = until the first words of the answer, "
                                            "Voice = until the answer was audible")

    def _on_partial(self, text: str) -> None:
        shown = text if len(text) <= 60 else "…" + text[-59:]
        self.hint_label.setText(f"“{shown}”")

    # ------------------------------------------------------------- dashboard

    def show_command_center(self, section: str = "") -> None:
        """The command-center view (left rail, conversation, context panel): created on first use, replays the chat."""
        if self.command_center is None:
            from app.ui.command_center.view import CommandCenterView

            view = CommandCenterView(self.controller, self)
            for role, text in self.chat.messages:
                if text:
                    view.chat.add_message(role, text)
            view.core_requested.connect(lambda: self.show_page(0))
            self.command_center = view
            self._cc_index = self.pages.addWidget(view)
            self._apply_stats_to_cc()
        self.pages.setCurrentIndex(self._cc_index)
        for widget in self._bottom_bar:        # the view has its own command bar
            widget.setVisible(False)
        if section:
            self.command_center.show_section(section)
        self.command_center.command.focus()

    def _apply_stats_to_cc(self) -> None:
        if self.command_center is None:
            return
        try:
            cpu = psutil.cpu_percent(None)
            ram = psutil.virtual_memory().percent
        except Exception:
            cpu = ram = None
        self.command_center.on_stats(cpu, ram, self._gpu_text if getattr(self, "_gpu_available", False) else None)

    def show_page(self, index: int) -> None:
        for widget in getattr(self, "_bottom_bar", []):
            if widget is not self.live_box:
                widget.setVisible(True)
        self.pages.setCurrentIndex(index)
        for i, tab in enumerate(self.tabs[:2]):
            tab.set_current(i == index)
        if hasattr(self, "page_indicator"):
            self.page_indicator.setText(("CORE LIVE", "SYSTEM")[index] if index < 2 else "CORE LIVE")

    def _on_tool_event(self, text: str) -> None:
        self.events.add("tool", text)
        name = text.split("]", 1)[-1].strip().split("(", 1)[0].strip()
        if name:
            self._last_action = name[:24]
            self._update_live_box()

    def _send_comms(self) -> None:
        """The conversation panel's own input: the same pipeline as the bottom bar (text, then Enter)."""
        text = self.comms_input.text().strip()
        if not text:
            return
        self.comms_input.clear()
        self.controller.send_text(text)

    def _apply_core_settings(self) -> None:
        s = self.controller.ctx.settings
        if hasattr(self.orb, "apply_settings"):
            self.orb.apply_settings(s.visual_quality, s.core_speed, s.glow_intensity, s.reduced_motion)
        self._apply_hud_rings()
        from app.ui.dashboard_fx import apply_fx_settings

        apply_fx_settings(self)
        if self.command_center is not None:
            self.command_center.apply_style()

    def _apply_hud_rings(self) -> None:
        """The flat rings, gauges and ticks around the core: off by default (Settings brings them back)."""
        on = bool(getattr(self.controller.ctx.settings, "core_hud_rings", False))
        model = getattr(self.orb, "model", self.orb)
        if hasattr(model, "hud_rings"):
            model.hud_rings = on

    def _on_level(self, level: float) -> None:
        self.level_bars.push(level)
        self.spectrum.push(level)

    def _update_live_box(self) -> None:
        name = self.controller.ctx.settings.assistant_name.upper()
        state = STATE_LABELS.get(getattr(self, "_last_state", "IDLE") or "IDLE", "Ready").upper()
        voice = "ON" if self.controller.hands_free is not None else "OFF"
        text = f"{name} LIVE v{__version__}\nSTATE: {state}   LIVE VOICE: {voice}\nLAST ACTION: {self._last_action}"
        self.live_box.setText(text)
        if hasattr(self, "live_box_side"):
            self.live_box_side.setText(text)

    def _update_dashboard_stats(self) -> None:
        """The dashboard mirrors the SYSTEM page's meters (same measurements, no extra work)."""
        cpu, ram, disk = self.cpu_gauge.value, self.ram_gauge.value, self.disk_gauge.value
        self.perf["cpu"].set_value(cpu, self.cpu_gauge.caption)
        self.donut_cpu.set_value(cpu)
        self.donut_ram.set_value(ram)
        battery_now = self.battery_gauge.value
        self.meters.set_values([cpu, ram, self._gpu_util if self._gpu_available else None, disk,
                                getattr(self, "_gpu_mem_percent", None) if self._gpu_available else None, battery_now])
        self.freq_card.set_values(self.cpu_gauge.caption.split("·")[-1].strip() if self.cpu_gauge.caption else "--",
                                  cpu, battery_now)
        ai_state = self.overview["ai"].text() if hasattr(self, "overview") else "--"
        secure = self.overview["actions"].text() if hasattr(self, "overview") else "--"
        self.engine_line.setText(f"<span style='color:{CYAN}'>●</span> AI ENGINE: {ai_state} · CPU {cpu or 0:.0f}%"
                                 f" &nbsp;&nbsp;<span style='color:{ORANGE}'>●</span> SECURITY: {secure}")
        self.meters_caption.setText(f"{self.cpu_gauge.caption or 'FREQUENCY --'}   ·   BATTERY "
                                    f"{'AC' if battery_now is None else f'{battery_now:.0f}%'}".upper())
        self.perf["ram"].set_value(ram, self.ram_gauge.caption)
        self.perf["disk"].set_value(disk, self.disk_gauge.caption)
        if self._gpu_available:
            self.perf["gpu"].set_value(self._gpu_util, self._gpu_text.split(" · ", 1)[-1] if self._gpu_util else "")
        self.link["down"].set(self.rows["down"].text(), "info")
        self.link["up"].set(self.rows["up"].text(), "info")
        self.link["uptime"].set(self.rows["uptime"].text(), "info")
        offline = getattr(self, "_offline", False)
        self.link["network"].set("offline" if offline else "online", "bad" if offline else "ok")
        battery = self.battery_gauge.value
        if battery is None:
            self.power.set_value(100, "AC", "MAINS POWER", GREEN)
        else:
            color = COLOR["danger"] if battery <= 15 else GREEN
            self.power.set_value(battery, f"{battery:.0f}%", self.battery_gauge.caption.upper()[:14], color)
        self._stats_refreshes = getattr(self, "_stats_refreshes", 0) + 1
        if self._stats_refreshes % 5 == 1:  # every few ticks: counts from the database, module states
            self._refresh_dashboard(counts=True)

    def _refresh_approvals(self) -> None:
        pending = 0
        try:
            from app.memory.suggestions import SuggestionStore

            pending = len(SuggestionStore(self.controller.ctx.database).pending())
        except Exception:
            pass
        if self._confirm_dialog is None:
            self.memory_action.setText(f"Memory ({pending} to review)..." if pending else "Memory...")
            self.approval_label.setText(f"No actions waiting.  {pending} memory suggestion(s) to review (MEMORY tab)."
                                        if pending else "No actions waiting.")

    def _refresh_dashboard(self, counts: bool = False) -> None:
        """Real states only: from the settings, the controller and the database."""
        if not hasattr(self, "overview"):
            return
        c, s = self.controller, self.controller.ctx.settings
        model = getattr(self, "_model_name", "")
        ai_ok = self._ai_ok
        self.overview["ai"].set("online" if ai_ok else "offline" if ai_ok is False else "starting",
                                "ok" if ai_ok else "bad" if ai_ok is False else "warn")
        self.overview["model"].set(model[:18] or "--", "info" if model else "off")
        speech = self.rows["voice"].text()
        self.overview["speech"].set(speech.split(" · ")[-1][:14] if speech else "--",
                                    "warn" if "loading" in speech.lower() else "ok")
        self.overview["voice"].set(s.tts_engine if s.tts_enabled else "muted", "ok" if s.tts_enabled else "off")
        offline = getattr(self, "_offline", False) or s.forced_offline
        self.overview["internet"].set("offline" if offline else "online", "warn" if offline else "ok")
        permissions = getattr(c.ctx.config, "permissions", None)
        guarded = getattr(permissions, "confirm_moderate", True)
        self.overview["actions"].set("protected" if guarded else "open", "warn" if guarded else "bad")
        self.modules["voice"].set("ready" if s.microphone_enabled else "off", "ok" if s.microphone_enabled else "off")
        self.modules["hands_free"].set("on" if c.hands_free is not None else "off",
                                       "ok" if c.hands_free is not None else "off")
        self.modules["screen"].set("enabled" if s.screen_capture_enabled else "off",
                                   "ok" if s.screen_capture_enabled else "off")
        self.modules["browser"].set("enabled" if s.browser_enabled else "off", "ok" if s.browser_enabled else "off")
        linked = bool(s.home_assistant_url and s.home_assistant_token)
        self.modules["home"].set("linked" if linked else "--", "ok" if linked else "off")
        hub = getattr(getattr(c, "executor", None), "mcp_hub", None)
        servers = len(getattr(hub, "servers", {}) or {}) if hub is not None else 0
        self.modules["mcp"].set(f"{servers} linked" if servers else "--", "ok" if servers else "off")
        remote_on = getattr(c, "remote", None) is not None
        self.modules["remote"].set("on" if remote_on else "off", "ok" if remote_on else "off")
        notes_on = getattr(c, "notes", None) is not None
        self.modules["notes"].set("recording" if notes_on else "--", "warn" if notes_on else "off")
        self.cards["ai"].set(model or "no model yet", "online" if ai_ok else "offline" if ai_ok is False else "starting",
                             "ok" if ai_ok else "bad" if ai_ok is False else "warn")
        self.cards["voice"].set(speech or "--", "loading" if "loading" in speech.lower() else "ready",
                                "warn" if "loading" in speech.lower() else "ok")
        self.cards["security"].set("Asks before risky actions" if guarded else "Actions run without asking",
                                   "protected" if guarded else "open", "ok" if guarded else "bad")
        if counts:
            db = c.ctx.database
            try:
                from app.memory.manager import MemoryManager
                from app.memory.suggestions import SuggestionStore

                facts = len(MemoryManager(db).all(10000))
                waiting = len(SuggestionStore(db).pending())
                self.cards["memory"].set(f"{facts} fact(s)" + (f" · {waiting} to review" if waiting else ""),
                                         "review" if waiting else "stored", "warn" if waiting else "ok")
            except Exception:
                self.cards["memory"].set("--", "--", "off")
            try:
                from app.automation.tasks import AutomationStore

                active = [task for task in AutomationStore(db).all() if task.enabled]
                self.cards["automations"].set(f"{len(active)} active", "running" if active else "none",
                                              "ok" if active else "off")
            except Exception:
                self.cards["automations"].set("--", "--", "off")
            self._refresh_approvals()
        self._update_live_box()
        self._refresh_header()

    def _refresh_header(self) -> None:
        ai_ok = self._ai_ok
        ai = ("ONLINE", COLOR["ok"]) if ai_ok else ("OFFLINE", COLOR["danger"]) if ai_ok is False else ("STARTING", ORANGE)
        voice = ("ON", COLOR["ok"]) if self.controller.hands_free is not None else ("OFF", DASH_DIM)
        offline = getattr(self, "_offline", False)
        net = ("OFFLINE", ORANGE) if offline else ("ONLINE", COLOR["ok"])
        part = lambda k, v: f"{k}: <span style='color:{v[1]}'>{v[0]}</span>"  # noqa: E731
        self.header_status.setText(" &nbsp; ".join((part("AI CORE", ai), part("LIVE VOICE", voice),
                                                    part("NETWORK", net))))

    # --------------------------------------------------------------- windows

    def open_settings(self) -> None:
        from app.ui.settings_dialog import SettingsDialog
        from app.voice.audio import AudioError, list_devices

        microphones: list[str] = []
        speakers: list[str] = []
        try:
            for device in list_devices():
                if device.max_input_channels and device.name not in microphones:
                    microphones.append(device.name)
                if device.max_output_channels and device.name not in speakers:
                    speakers.append(device.name)
        except AudioError:
            pass
        ctx = self.controller.ctx
        dialog = SettingsDialog(
            ctx.settings, ctx.paths.env_file, models=self.controller.installed_models(),
            microphones=microphones, speakers=speakers, on_saved=self.controller.apply_settings,
            on_restart=self._restart_for_settings, parent=self,
        )
        dialog.exec()
        wanted = bool(getattr(self.controller.ctx.settings, "start_fullscreen", True))
        if wanted != self.wants_full_screen and self.isVisible():
            self.set_full_screen(wanted)                       # "Start full screen" changed: apply it now

    def _restart_for_settings(self, names: list[str]) -> None:
        """A saved setting is only read at start-up (speech model, ports, devices...): Jarvis restarts itself to apply
        it - nothing to click. The conversation and everything saved carry over."""
        if getattr(self, "_settings_restart_pending", False):
            return
        self._settings_restart_pending = True
        shown = ", ".join(n.lower().replace("_", " ") for n in names[:4]) + ("..." if len(names) > 4 else "")
        self.chat.add_message("notice", f"Saved. Applying {shown} - Jarvis restarts in a moment.")
        QTimer.singleShot(1500, self.restart_jarvis)

    def open_setup_wizard(self) -> None:
        from app.ui.setup_wizard import SetupWizard

        if getattr(self, "_wizard", None) is not None and self._wizard.isVisible():
            self._wizard.raise_()
            return
        self._wizard = SetupWizard(self.controller, parent=self)
        self._wizard.setModal(False)
        self._wizard.show()

    def make_jarvis_model(self) -> None:
        """One model called "jarvis" (from the best installed one); optionally delete the others."""
        from PySide6.QtWidgets import QCheckBox, QMessageBox

        try:
            base, plan = self.controller.jarvis_model_plan()
        except Exception as exc:  # Ollama not running...
            QMessageBox.warning(self, "Jarvis model", f"Could not ask Ollama for the installed models: {exc}")
            return
        if base is None:
            QMessageBox.information(self, "Jarvis model", "No chat model is installed yet. Run the setup wizard "
                                    "(Menu -> Setup wizard) to download one first.")
            return
        box = QMessageBox(self)
        box.setWindowTitle("Make the Jarvis model")
        others = [n for n in plan.delete if n != base.name] + [base.name]
        box.setText(f"Make one model called \"jarvis\" from {base.name} and use it from now on?\n\n"
                    "It shares the files of that model: it takes seconds and no extra disk space. To upgrade later, "
                    "install a better model and do this again - \"jarvis\" is replaced.")
        delete_box = QCheckBox("Also delete the other chat models: " + ", ".join(others))
        delete_box.setChecked(False)
        box.setCheckBox(delete_box)
        box.setInformativeText("Kept anyway: " + (", ".join(n for n in plan.keep if n != base.name) or "nothing else")
                               + " (search, screen and fast/smart models do other jobs).")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self.controller.make_jarvis_model(base.name, others if delete_box.isChecked() else [])

    def _on_jarvis_action(self, action: str) -> None:
        """Jarvis's own screens and features, asked for by voice."""
        c = self.controller
        actions = {
            "open_settings": self.open_settings, "open_memory": self.open_memory, "open_privacy": self.open_privacy,
            "open_logs": self.open_logs, "open_automations": self.open_automations,
            "mission_control": self.open_mission_control, "open_notifications": self.open_notifications,
            "emergency_controls": self.open_emergency, "evolution_center": self.open_evolution_center,
            "task_center": self.open_task_center, "my_day": self.open_my_day,
            "workflow_studio": self.open_workflow_studio, "security_center": self.open_security_center,
            "show_system": lambda: self.show_page(1), "show_dashboard": lambda: self.show_page(0),
            "enroll_voice": c.enroll_voice, "teach_wake_word": c.teach_wake_word, "train_voice": c.train_voice,
            "make_jarvis_model": self._make_jarvis_model_by_voice, "self_test": self.run_self_test,
            "speed_report": c.speed_report, "check_updates": self.check_updates, "phone_remote": self.show_phone_remote,
            "new_conversation": self.new_conversation, "hide_window": self.hide, "show_window": self.bring_back,
            "setup_wizard": self.open_setup_wizard, "hands_free_off": lambda: self._toggle_hands_free(False),
            "mute_microphone": lambda: self.controller.set_mic_muted(True),
            "close_dialog": self._close_dialog, "health_report": c.health_report, "use_best_model": c.use_best_model,
            "full_screen": lambda: self.set_full_screen(True), "exit_full_screen": lambda: self.set_full_screen(False),
            "stay_on_top": lambda: self.set_always_on_top(True), "not_on_top": lambda: self.set_always_on_top(False),
            "compare_models": c.compare_models, "restart_jarvis": self.restart_jarvis,
            "window_overview": lambda: self.open_overview("windows"), "tab_overview": lambda: self.open_overview("tabs"),
            "phone_screen": self.open_phone_screen, "workspaces": self.open_workspaces,
        }
        handler = actions.get(action)
        if handler is not None:
            QTimer.singleShot(200, handler)  # after the reply started, so the dialog doesn't cover it

    def open_overview(self, mode: str = "windows") -> None:
        """'Show me my screen' / 'show my tabs': every open window (or browser tab) flies into a grid; pick one."""
        from app.tools import winapi
        from app.ui import dwm
        from app.ui.window_overview import WindowOverview, activate, tab_items, window_items

        if not winapi.is_windows():
            self.chat.add_message("notice", "The window overview works on Windows.")
            return
        try:
            items = tab_items() if mode == "tabs" else window_items()
        except Exception as exc:
            self.chat.add_message("error", f"I couldn't list the open {'tabs' if mode == 'tabs' else 'windows'}: {exc}")
            return
        if not items:
            self.chat.add_message("notice", "No browser tabs are open." if mode == "tabs" else "No other windows are open.")
            return
        if getattr(self, "_overview", None) is not None:
            self._overview.dismiss()
        overview = WindowOverview(items, mode, thumbnail_factory=dwm.Thumbnail)
        self._overview = overview

        def chosen(item) -> None:
            try:
                activate(item)
            except Exception as exc:
                self.chat.add_message("error", f"I couldn't bring that window to the front: {exc}")

        overview.chosen.connect(chosen)
        overview.closed.connect(lambda: setattr(self, "_overview", None))
        overview.open()

    def restart_jarvis(self) -> None:
        """Start a fresh Jarvis (it waits for this one to exit) and quit, so the latest code runs without a PC restart."""
        import os

        from PySide6.QtWidgets import QApplication

        from app.core import relaunch

        try:
            relaunch.start_replacement(os.getpid(), minimized=not self.isVisible())
        except OSError as exc:
            self.chat.add_message("error", f"I couldn't restart myself: {exc}")
            return
        self.allow_close = True
        QTimer.singleShot(2500, QApplication.quit)  # let the spoken reply finish first

    def _close_dialog(self) -> None:
        from PySide6.QtWidgets import QApplication, QDialog

        dialog = QApplication.activeModalWidget() or getattr(self, "_wizard", None)
        if isinstance(dialog, QDialog) and dialog.isVisible():
            dialog.reject()

    def _make_jarvis_model_by_voice(self) -> None:
        """By voice: make "jarvis" from the best model; deleting other models is only offered on screen."""
        try:
            base, _plan = self.controller.jarvis_model_plan()
        except Exception as exc:
            self.chat.add_message("error", f"Could not ask Ollama for the models: {exc}")
            return
        if base is None:
            self.chat.add_message("error", "No chat model is installed yet.")
            return
        self.controller.make_jarvis_model(base.name, [])

    def show_phone_remote(self) -> None:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QMessageBox

        url = self.controller.start_remote()
        if url is None:
            QMessageBox.information(self, "Phone remote", "The phone remote is off. Turn on 'Phone remote' in "
                                    "Settings (REMOTE_ENABLED=true), then open this again.")
            return
        QGuiApplication.clipboard().setText(url)
        from app.ui.phone_setup import PhoneSetupDialog

        code = self.controller.pairing_code() or "------"
        fingerprint = getattr(self.controller, "remote_fingerprint", "")
        paired = len(getattr(getattr(self.controller, "pairing", None), "devices", {}) or {})
        away = getattr(getattr(self.controller, "remote", None), "away_url", None)
        PhoneSetupDialog(url, away, code, fingerprint, paired, parent=self).exec()

    def run_self_test(self) -> None:
        self.chat.add_message("notice", "Running the self-test... (about 30 seconds)")
        self.controller.run_self_test()

    def export_diagnostics(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        from app.utils.diagnostics import export_diagnostics

        path, _ = QFileDialog.getSaveFileName(self, "Save diagnostics", "jarvis-diagnostics.zip", "Zip (*.zip)")
        if not path:
            return
        try:
            saved = export_diagnostics(self.controller.ctx, path)
            self.chat.add_message("notice", f"Diagnostics saved to {saved} (passwords, keys and tokens removed).")
        except Exception as exc:  # shown to the user
            self.chat.add_message("error", f"Could not export diagnostics: {exc}")

    def check_updates(self) -> None:
        self.controller.check_for_updates(manual=True)

    def _on_update_found(self, info) -> None:
        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            self, "Update available", f"Jarvis {info.version} is available.\n\n{info.notes[:600]}\n\n"
            "Download and install it now? (The download is checked against its SHA-256 and code signature first.)")
        if answer != QMessageBox.StandardButton.Yes:
            return
        import tempfile
        from pathlib import Path

        from app.tools import winapi
        from app.utils import updates

        try:
            installer = updates.download(info, Path(tempfile.gettempdir()) / "JarvisUpdate",
                                         require_signature=self.controller.ctx.settings.update_require_signature)
            from app import __version__
            from app.core import update_guard

            try:   # a verified copy of the data first; if it cannot be made, the installer is NOT started
                update_guard.begin(self.controller.ctx.paths, __version__, info.version, info.sha256)
            except Exception as exc:
                self.chat.add_message("error", f"The update was not started because the safety backup failed: {exc}")
                return
            winapi.start_file(str(installer))
            self.chat.add_message("notice", "A safety backup of your data was made and the installer has started. "
                                            "Jarvis will close when it updates and check itself when it starts again.")
        except Exception as exc:  # shown to the user
            self.chat.add_message("error", f"The update failed: {exc}")

    def open_memory(self) -> None:
        from app.memory.manager import MemoryManager
        from app.ui.memory_dialog import MemoryDialog

        from app.memory.suggestions import SuggestionStore

        database = self.controller.ctx.database
        MemoryDialog(MemoryManager(database), parent=self, suggestions=SuggestionStore(database)).exec()

    def open_privacy(self) -> None:
        from app.ui.privacy_dialog import PrivacyDialog

        PrivacyDialog(self.controller.ctx, on_changed=self.controller.privacy_changed, parent=self).exec()

    def open_automations(self) -> None:
        from app.automation.tasks import AutomationStore
        from app.ui.automation_dialog import AutomationDialog

        dialog = AutomationDialog(AutomationStore(self.controller.ctx.database), self.controller, parent=self)
        dialog.exec()

    def open_notifications(self) -> None:
        from app.ui.notification_dialog import NotificationDialog

        center = getattr(self.controller, "notifications", None)
        if center is None:
            return
        NotificationDialog(center, run_action=self.controller.send_text, parent=self).exec()
        self.notifications_action.setText("Notifications...")

    def open_emergency(self) -> None:
        from app.ui.notification_dialog import EmergencyDialog

        EmergencyDialog(self.controller.emergency_stop, self.controller.set_emergency_switch, parent=self).exec()

    def open_models(self) -> None:
        from app.ui.models_dialog import ModelsDialog

        ModelsDialog(self.controller, parent=self).exec()

    def open_workspaces(self) -> None:
        from app.ui.workspaces import WorkspacesDialog

        WorkspacesDialog(self, parent=self, send_command=self.controller.send_text).exec()

    def open_phone_screen(self) -> None:
        """A phone sharing its screen (it starts the sharing; Android asks it every time). Not modal: keep working."""
        from app.ui.phone_screen_view import PhoneScreenWindow

        window = getattr(self, "_phone_screen_window", None)
        if window is None or not window.isVisible():
            window = self._phone_screen_window = PhoneScreenWindow(
                self.controller.phone_screens(), parent=self, ask_to_share=self.controller.ask_phone_to_share)
        window.show()
        window.raise_()

    def open_device_manager(self) -> None:
        from app.ui.device_manager import DeviceManagerDialog

        DeviceManagerDialog(self.controller, parent=self).exec()

    def open_knowledge(self) -> None:
        from app.ui.knowledge_dialog import KnowledgeDialog

        KnowledgeDialog(self.controller.executor.context, parent=self).exec()

    def open_context_panel(self) -> None:
        from app.ui.context_panel import ContextPanel

        ContextPanel(self.controller.executor.context, parent=self).exec()

    def open_mobile_devices(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from app.ui.mobile_devices import MobileDevicesDialog

        registry = self.controller.mobile_registry()
        if registry is None:
            QMessageBox.information(self, "Mobile devices", "Mobile devices need the database.")
            return
        from app.remote import apk

        remote = getattr(self.controller, "remote", None)
        apk_link = f"{remote.url.rstrip('/')}/mobile/app.apk" if remote is not None else None
        MobileDevicesDialog(registry, self.controller.mobile_pairing_link, parent=self,
                            sessions=self.controller.mobile_sessions(), app_info=apk.info(),
                            apk_link=apk_link, install_qr=apk_link).exec()

    def _mobile_pending(self, device) -> None:
        """A phone just paired: ask now whether to trust it (it can do nothing until then)."""
        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(self, "JARVIS Mobile", f"{device.name} wants to connect.\n\nFingerprint:\n"
                                      f"{device.fingerprint}\n\nTrust it? Only say yes if the phone shows the same "
                                      "fingerprint and it's yours.")
        registry = self.controller.mobile_registry()
        if registry is not None:
            registry.set_trust(device.id, "trusted" if answer == QMessageBox.StandardButton.Yes else "revoked")

    def open_plugins(self) -> None:
        from app.ui.plugins_dialog import PluginsDialog

        ctx = self.controller.ctx
        PluginsDialog(ctx.paths.data_dir / "plugins", ctx.settings.plugins_enabled, parent=self).exec()

    def open_evolution_center(self) -> None:
        from app.ui.evolution_center import EvolutionCenter

        if self.controller.ctx.database is None:
            return
        EvolutionCenter(self.controller.ctx.database, self.controller.send_text, parent=self).exec()

    def open_mission_control(self) -> None:
        from app.ui.mission_control import MissionControlDialog

        if self.controller.ctx.database is None:
            return
        MissionControlDialog(self.controller.ctx.database,
                             on_undo=lambda: self.controller.send_text("undo your last action"), parent=self).exec()

    def open_life_log(self) -> None:
        from app.ui.personal_dialog import PersonalDialog

        if self.controller.ctx.database is None:
            return
        PersonalDialog(self.controller.executor.context, parent=self).exec()

    def open_my_day(self) -> None:
        from app.ui.my_day import MyDayDialog

        if self.controller.ctx.database is None:
            return
        MyDayDialog(self.controller.executor.context, self.controller.send_text, parent=self).exec()

    def open_gestures(self) -> None:
        """Hand-gesture control page (command center > Gestures)."""
        self.show_command_center("gestures")

    def open_security_center(self) -> None:
        from app.ui.security_center import SecurityCenterDialog

        c = self.controller
        if c.ctx.database is None:
            return
        SecurityCenterDialog(c.ctx.database, c.ctx.settings, open_phones=self.open_mobile_devices,
                             open_privacy=self.open_privacy, open_emergency=self.open_emergency, parent=self,
                             live=c.live_privacy_state).exec()

    def open_workflow_studio(self) -> None:
        from app.ui.workflow_studio import WorkflowStudioDialog

        if self.controller.ctx.database is None:
            return
        WorkflowStudioDialog(self.controller.executor.context, self.controller.send_text, parent=self).exec()

    def open_task_center(self) -> None:
        from app.ui.task_center import TaskCenterDialog

        if self.controller.ctx.database is None:
            return
        TaskCenterDialog(self.controller.ctx.database, self.controller.send_text, parent=self).exec()

    def open_diagnostics(self) -> None:
        from app.ui.diagnostics_dialog import DiagnosticsDialog

        def repair() -> str:
            from app.core import diagnostics

            done = diagnostics.repair(self.controller.ctx)
            return ("Repaired: " + "; ".join(done) + ".") if done else ""

        DiagnosticsDialog(self.controller.ctx, repair, parent=self).exec()

    def open_logs(self) -> None:
        from app.tools import winapi
        from app.tools.base import ToolError
        from app.ui.log_viewer import LogViewer

        def open_file(path: str) -> None:
            try:
                winapi.start_file(path)
            except ToolError as exc:
                self.chat.add_message("error", str(exc))

        LogViewer(self.controller.ctx.log_file, open_file=open_file, parent=self).exec()

    def new_conversation(self) -> None:
        self.controller.reset_conversation()
        self.chat.add_message("notice", "Started a new conversation.")

    def _tick_clock(self) -> None:
        now = datetime.now()
        self.clock.setText(time12(now, True))
        self.date.setText(now.strftime("%A · %d %B %Y").upper())
        if now.second % 2 == 0:                       # `python run.py --restart` from a terminal asks here
            self._check_restart_request()

    def _check_restart_request(self) -> None:
        try:
            from app.core import relaunch

            wanted = relaunch.take_request(self.controller.ctx.paths.data_dir)
        except Exception:
            return
        if wanted:
            self.restart_jarvis()

    def _update_network(self) -> None:
        from app.utils import network

        settings = self.controller.ctx.settings
        if settings.offline_mode == "auto":
            network.MONITOR.refresh_in_background()
        offline = network.is_offline_now(settings)
        self.offline_indicator.setText("OFFLINE MODE" if settings.forced_offline else "NO INTERNET")
        self.offline_indicator.setVisible(offline)
        self._offline = offline
        self._set_pill()

    def _set_pill(self) -> None:
        """● ONLINE / ● NO INTERNET / ● AI OFFLINE, top left."""
        offline = getattr(self, "_offline", False)
        forced = self.controller.ctx.settings.forced_offline
        if self._ai_ok is False:
            text, color = "● AI OFFLINE", COLOR["danger"]
        elif forced:
            text, color = "● OFFLINE MODE", "#94a3b8"
        elif offline:
            text, color = "● NO INTERNET", COLOR["warn"]
        elif self._ai_ok is None:
            text, color = "● STARTING", COLOR["dim"]
        else:
            text, color = "● ONLINE", COLOR["ok"]
        self.status_pill.setText(text)
        if hasattr(self, "header_status"):
            self._refresh_header()
        self.status_pill.setStyleSheet(f"color:{color}; border:1px solid {color}; background:rgba(2,6,14,0.8); border-radius:10px;")

    def _tick_stats(self) -> None:
        """Measure in a worker thread; _apply_stats shows the numbers on this thread (never blocks it)."""
        self._update_network()
        worker = getattr(self, "_stats_worker", None)
        if worker is not None and worker.is_alive():
            return  # the last measurement is still running: skip this tick rather than pile up
        self._stats_ticks += 1
        with_processes = self._stats_ticks % 2 == 1  # every other tick: listing processes costs more
        if with_processes:
            self._update_gpu()
        import threading as _threading

        from app.ui.stats_sampler import take

        drive = _system_drive()

        if getattr(self, "_stats_helper", None) is None:
            from app.ui.stats_process import StatsHelper

            self._stats_helper = StatsHelper()
        helper = self._stats_helper

        def work() -> None:
            try:
                sample = take(drive, with_processes, helper)
            except Exception:
                return
            self.stats_ready.emit(sample)  # queued to the window's thread

        self._stats_worker = _threading.Thread(target=work, name="dashboard-stats", daemon=True)
        self._stats_worker.start()

    def _apply_stats(self, s) -> None:
        cpu, cores = s.cpu, s.cores
        self.cpu_gauge.set_value(cpu, color=COLOR["danger"] if cpu >= 90 else STATE_COLORS["IDLE"])
        self.cpu_history.add(cpu)
        self.orb.set_telemetry("cpu", cpu)
        speed = f" · {s.freq_ghz:.1f} GHz" if s.freq_ghz else ""
        self.cpu_label.setText(f"{cpu:.0f}% · {cores} cores{speed}")
        self.cpu_gauge.caption = f"{cores} cores{speed}"

        memory = s.memory
        used = (memory.total - memory.available) / 1024**3
        self.ram_gauge.set_value(memory.percent, caption=f"{used:.1f} / {memory.total / 1024**3:.0f} GB")
        self.orb.set_telemetry("ram", memory.percent, f"{memory.percent:.0f}% · {used:.1f} GB")
        self.ram_label.setText(f"{used:.1f} / {memory.total / 1024**3:.1f} GB")

        battery = s.battery
        if battery is None:
            self.battery_gauge.set_value(None, "AC", caption="no battery")
            self.battery_label.setText("Mains power")
            self.orb.set_telemetry("battery", None, "AC POWER")
        else:
            charging = battery.power_plugged
            colour = COLOR["danger"] if battery.percent <= 15 and not charging else COLOR["ok"]
            if charging:
                detail = "charging" if battery.percent < 100 else "full"
            elif battery.secsleft and battery.secsleft > 0 and battery.secsleft != psutil.POWER_TIME_UNLIMITED:
                detail = f"{_duration(battery.secsleft)} left"
            else:
                detail = "on battery"
            self.battery_gauge.set_value(battery.percent, caption=detail, color=colour)
            self.battery_label.setText(f"{battery.percent:.0f}% · {detail}")
            self.orb.set_telemetry("battery", battery.percent, f"{battery.percent:.0f}% · {'⚡' if charging else detail}")

        disk = s.disk
        if disk is not None:
            self.rows["disk"].set(f"{disk.used / 1024**3:.0f} / {disk.total / 1024**3:.0f} GB ({disk.percent:.0f}%)")
            self.disk_gauge.set_value(disk.percent, caption=f"{disk.free / 1024**3:.0f} GB free",
                                      color=COLOR["danger"] if disk.percent >= 92 else COLOR["cyan"])
        else:
            self.rows["disk"].set("--")
        if s.net is not None:
            now, sent, received = s.net
            if self._net_last is not None and now > self._net_last[0]:
                elapsed = now - self._net_last[0]
                down = max(0, received - self._net_last[2]) / elapsed
                up = max(0, sent - self._net_last[1]) / elapsed
                self.rows["down"].set(_human_rate(down))
                self.rows["up"].set(_human_rate(up))
                self.net_history.add(down, up)
            self._net_last = (now, sent, received)
        if s.boot_time:
            self.rows["uptime"].set(_duration(time.time() - s.boot_time))
        if s.processes is not None:
            self._show_processes(s.processes)
        if self._gpu_available:
            self.rows["gpu"].set(self._gpu_text)
            self.gpu_gauge.set_value(self._gpu_util, caption=self._gpu_text.split(" · ", 1)[-1]
                                     if self._gpu_util is not None else "")
            self.orb.set_telemetry("gpu", self._gpu_util, self._gpu_text if self._gpu_util is not None else "--")
        else:
            self.orb.set_telemetry("gpu", None, "N/A")
        if s.pids is not None:
            self.rows["procs"].set(str(s.pids))
        self._update_dashboard_stats()
        if self.command_center is not None:
            self.command_center.on_stats(cpu, memory.percent, self._gpu_text if self._gpu_available else None)

    _IGNORED_PROCESSES = frozenset({"system idle process", "idle", "system", "registry", "memory compression"})

    def _show_processes(self, busiest) -> None:
        """The apps using the most CPU right now (measured in stats_sampler, off this thread)."""
        top_cpu = max([1.0] + [b[0] for b in busiest[:5]])
        for row, bar, item in zip(self.process_rows, self.process_bars, busiest + [None] * len(self.process_rows)):
            if item is None:
                row.name.setText("")
                row.set("")
                bar.set_value(0)
                continue
            cpu, rss, name = item
            row.name.setText(name[:18])
            row.set(f"{cpu:4.1f}%  {rss / 1024**2:5.0f} MB")
            bar.set_value(100 * cpu / top_cpu)

    def _update_gpu(self) -> None:
        """NVIDIA GPU load, memory and temperature (nvidia-smi, in a background thread: it takes ~0.1 s)."""
        if not self._gpu_available or (self._gpu_thread is not None and self._gpu_thread.is_alive()):
            return

        def work() -> None:
            from app.tools.system import gpu_info

            try:
                gpus = gpu_info().get("gpus") or []
            except Exception:
                gpus = []
            if not gpus:
                self._gpu_text, self._gpu_util = "--", None
                return
            g = gpus[0]
            try:
                used, total = float(g["memory_used_mb"]) / 1024, float(g["memory_total_mb"]) / 1024
                self._gpu_text = f"{g['utilization_percent']}% · {used:.1f}/{total:.0f} GB · {g['temperature_c']}°C"
                self._gpu_util = float(g["utilization_percent"])
                self._gpu_mem_percent = 100.0 * used / total if total else None
            except (KeyError, ValueError):
                self._gpu_text, self._gpu_util = "--", None

        import threading as _threading

        self._gpu_thread = _threading.Thread(target=work, name="gpu-stats", daemon=True)
        self._gpu_thread.start()

    # ------------------------------------------------------------- visibility

    def _dark_title_bar(self) -> None:
        """Ask Windows 11 for a dark title bar to match (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
        if self._dark_title_bar_done or sys.platform != "win32":
            return
        self._dark_title_bar_done = True
        try:
            import ctypes

            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(int(self.winId()), 20, ctypes.byref(value),
                                                       ctypes.sizeof(value))
        except Exception:
            pass

    def _update_mini_orb(self) -> None:
        if self.mini_orb is None:
            return
        away = self.isHidden() or self.isMinimized()
        if away and not self.mini_orb.isVisible():
            self.mini_orb.pop_in()
        elif not away:
            self.mini_orb.setVisible(False)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.WindowStateChange:
            self._update_mini_orb()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._update_mini_orb()
        self._dark_title_bar()
        self._clock_timer.start(1000)
        self._stats_timer.start()
        self._tick_clock()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if not self.allow_close:
            self._update_mini_orb()
        self._clock_timer.stop()
        self._stats_timer.stop()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.allow_close:
            if self.mini_orb is not None:
                self.mini_orb.close()
            helper = getattr(self, "_stats_helper", None)
            if helper is not None:
                helper.close()
            event.accept()
            return
        event.ignore()  # the X button hides to the tray; "Exit" in the tray quits
        self.hide()
        self.hidden_to_tray.emit()
