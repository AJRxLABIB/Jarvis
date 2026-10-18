"""Settings window. Every field maps to a .env setting; saving validates first and
keeps the rest of .env (comments included) as it was."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from app.config.env_writer import overridden_by_environment, update_env_file
from app.config.settings import (
    BROWSER_CHANNELS, ENV_NAMES, OCR_ENGINES, OFFLINE_MODES, WEB_BROWSERS, WAKE_WORD_ENGINES, SEARCH_PROVIDERS, STT_COMPUTE_TYPES, STT_DEVICES, STT_MODELS, VAD_ENGINES, ConfigError,
    Settings,
)

# Settings that take effect without restarting the app.
LIVE_FIELDS = frozenset({"web_browser", "chrome_profile", "edge_profile", "tts_speed", "tts_volume", "tts_enabled", "speak_typed_replies", "ai_model",
                         "start_with_windows", "direct_commands", "screen_capture_enabled", "screenshot_dir",
                         "screen_blocked_windows", "ocr_engine", "ocr_language", "vision_model", "hands_free",
                         "wake_word", "wake_word_engine", "wake_word_threshold", "wake_on_name", "wake_word_model", "follow_up_seconds",
                         "wake_sound", "home_location", "temperature_unit", "proactive_alerts",
                         "low_battery_percent", "barge_in", "voice_interrupt", "voice_confirm", "ai_fast_model",
                         "conversation_memory", "document_folders", "embedding_model", "update_manifest_url",
                         "speaker_verification", "speaker_threshold", "presence_detection", "presence_lock",
                         "presence_away_minutes", "proactive_suggestions", "idle_unload_minutes",
                         "home_assistant_url", "home_assistant_token", "github_token", "google_client_id", "google_client_secret", "custom_vocabulary", "stt_second_pass",
                         "ai_smart_model", "ai_coding_model", "auto_sort_downloads", "early_transcription",
                         "memory_suggestions", "tone_aware"})
# also applied without a restart (app/ui/controller.py apply_settings)
from app.config.live_fields import POLICY_FIELDS, REMOTE_FIELDS, STARTUP_ONLY_FIELDS  # noqa: E402

LIVE_FIELDS = LIVE_FIELDS | REMOTE_FIELDS | STARTUP_ONLY_FIELDS | POLICY_FIELDS | {
    "visual_quality", "core_speed", "glow_intensity", "reduced_motion", "core_hud_rings", "auto_sort_downloads", "weekly_health_check"}


@dataclass(frozen=True)
class FieldSpec:
    name: str  # Settings field name
    label: str
    kind: str  # text | secret | bool | choice | float | int | folder
    options: tuple[str, ...] = ()
    low: float = 0
    high: float = 100
    step: float = 1
    tip: str = ""


TABS: tuple[tuple[str, tuple[FieldSpec, ...]], ...] = (
    ("General", (
        FieldSpec("assistant_name", "Assistant name", "text"),
        FieldSpec("user_address", "Call me", "text", tip="Empty = no honorific. Examples: sir, Labib"),
        FieldSpec("user_name", "My name (start screen)", "text", tip="Shown on the start screen instead of THIS PC"),
        FieldSpec("home_location", "Home city", "text", tip="For the weather and the briefing, e.g. Dhaka or Dhaka, Bangladesh"),
        FieldSpec("temperature_unit", "Temperature", "choice", options=("celsius", "fahrenheit")),
        FieldSpec("proactive_alerts", "Speak up on its own (low battery, charger)", "bool"),
        FieldSpec("proactive_suggestions", "Suggest apps I usually open at this time", "bool"),
        FieldSpec("proactive_level", "How proactive Jarvis is", "choice",
                  options=("off", "minimal", "normal", "active", "high"),
                  tip="goals due, a paused mission, uncommitted work, things that keep failing"),
        FieldSpec("quiet_hours", "Quiet hours (suggestions not spoken)", "text", tip="e.g. 23:00-07:00; empty = none"),
        FieldSpec("proactive_max_per_hour", "Spoken suggestions per hour (at most)", "int", low=1, high=30),
        FieldSpec("proactive_categories", "Only these kinds (empty = all)", "text", tip="goal, mission, project, system"),
        FieldSpec("proactive_emergency_exceptions", "Urgent alerts even when proactive is off", "bool"),
        FieldSpec("low_battery_percent", "Low battery warning at (%)", "int", low=5, high=50),
        FieldSpec("auto_sort_downloads", "Sort new downloads into folders by type", "bool",
                  tip="Every few minutes; say 'undo sort downloads' to put them back"),
        FieldSpec("start_minimized", "Start in the tray", "bool"),
        FieldSpec("start_with_windows", "Start with Windows", "bool", tip="Needed for scheduled tasks while you work"),
        FieldSpec("speak_typed_replies", "Speak answers to typed messages", "bool"),
        FieldSpec("show_sources", "Show where an answer's facts came from (tools, memories)", "bool"),
        FieldSpec("learn_from_use", "Learn from every request (what worked, what you corrected)", "bool",
                  tip="Stays on this PC. Never in private mode. 'what have you learned' shows it; you can erase it."),
        FieldSpec("direct_commands", "Run 'open X' / 'close X' directly (no AI)", "bool"),
        FieldSpec("monitor_interval", "System stats refresh (seconds)", "float", low=1, high=60, step=1),
        FieldSpec("core_renderer", "3D core", "choice", options=("auto", "gl", "painter"),
                  tip="auto = real-time OpenGL 3D when the graphics card allows it, else the flat drawing; restart to switch"),
        FieldSpec("visual_quality", "3D quality", "choice", options=("auto", "low", "balanced", "high"),
                  tip="auto lowers the quality by itself if the animation can't keep up"),
        FieldSpec("core_speed", "Core animation speed", "float", low=0.3, high=2.0, step=0.1),
        FieldSpec("glow_intensity", "Glow intensity", "float", low=0.3, high=2.0, step=0.1),
        FieldSpec("reduced_motion", "Reduced motion (calmer core, no sway)", "bool"),
        FieldSpec("core_hud_rings", "Rings and gauges around the core (gold segments, ticks, CPU/battery arcs)", "bool"),
    )),
    ("AI", (
        FieldSpec("ai_model", "Model", "choice", tip="Empty = pick automatically from installed models"),
        FieldSpec("ai_temperature", "Creativity (temperature)", "float", low=0, high=2, step=0.1),
        FieldSpec("ai_context_window", "Context window (tokens)", "int", low=1024, high=262144, step=1024),
        FieldSpec("ai_fast_model", "Fast model for quick requests (optional)", "text",
                  tip="e.g. llama3.2:1b; the main model answers the harder questions"),
        FieldSpec("ai_smart_model", "Bigger model for hard questions (optional)", "choice",
                  tip="e.g. qwen3:8b: used for explain / write / compare / plan; everyday requests stay on the main model"),
        FieldSpec("ai_coding_model", "Coding model (optional)", "choice",
                  tip="e.g. qwen2.5-coder:7b: code, errors, tests, builds; without it the bigger model is used"),
        FieldSpec("embedding_model", "Embedding model (search by meaning)", "text", tip="ollama pull nomic-embed-text"),
        FieldSpec("conversation_memory", "Remember our conversations (on this PC)", "bool"),
        FieldSpec("clipboard_history", "Remember what I copy so I can search it later (text only; never passwords or card numbers)", "bool"),
        FieldSpec("memory_suggestions", "Suggest facts to remember (I accept each one)", "bool"),
        FieldSpec("tone_aware", "Notice when I sound rushed or tired (shorter answers)", "bool"),
        FieldSpec("document_folders", "Document folders (ask my documents)", "text", tip="Separate folders with ;"),
        FieldSpec("idle_unload_minutes", "Free the AI model's memory after idle (minutes, 0 = never)", "float",
                  low=0, high=720, step=5),
        FieldSpec("gpu_memory_saver", "Small GPU: keep the AI and speech both on the GPU", "bool"),
        FieldSpec("taint_lockdown", "After reading web pages, email or documents, don't even offer sending/saving tools", "bool"),
        FieldSpec("ai_auto_switch", "Switch models automatically when my benchmark results say it's safer", "bool"),
        FieldSpec("ai_think", "Let reasoning models think first (slower)", "bool"),
        FieldSpec("ollama_keep_alive", "Keep model loaded for", "text", tip="e.g. 10m, 1h, 0 (unload), -1 (always)"),
        FieldSpec("ai_request_timeout", "Request timeout (seconds)", "float", low=10, high=3600, step=10),
        FieldSpec("ollama_host", "Ollama address", "text"),
        FieldSpec("auto_start_ollama", "Start Ollama automatically if it isn't running", "bool"),
        FieldSpec("hide_ollama_window", "Minimise the Ollama window at start-up", "bool",
                  tip="Ollama keeps running in the background; Jarvis comes to the front"),
    )),
    ("Voice input", (
        FieldSpec("microphone_device", "Microphone", "choice", tip="Empty = Windows default"),
        FieldSpec("hands_free", "Always listening (no Talk button needed)", "bool",
                  tip="The microphone stays open; only the wake phrase is detected until you say it"),
        FieldSpec("wake_word", "Wake phrase", "text"),
        FieldSpec("wake_word_engine", "Wake phrase detection", "choice", options=WAKE_WORD_ENGINES,
                  tip="auto = small wake-word model for 'hey jarvis', speech recognition for other phrases"),
        FieldSpec("wake_on_name", "Also wake on just the name (\"Jarvis\")", "bool",
                  tip="Short sentences are checked on this PC for the name; anything else is dropped"),
        FieldSpec("wake_word_threshold", "Wake phrase strictness", "float", low=0.1, high=0.95, step=0.05,
                  tip="Lower = reacts more easily, but more false alarms"),
        FieldSpec("follow_up_seconds", "Stay in conversation for (s)", "float", low=0, high=900, step=10,
                  tip="After you wake it, no wake phrase is needed until it has heard no request for this long. "
                      "Say 'goodbye' to end sooner. 0 = say the wake phrase every time"),
        FieldSpec("personal_wake_check", "Check wake-ups against my recordings (Menu: Teach me my wake word)",
                  "bool", tip="Fewer false wake-ups from the TV or similar words"),
        FieldSpec("wake_sound", "Chime when it hears the wake phrase", "bool"),
        FieldSpec("barge_in", "Interrupt with the wake phrase while it speaks", "bool"),
        FieldSpec("voice_interrupt", "Talk over it: \"stop\" or a new command (GPU)", "bool"),
        FieldSpec("voice_confirm", "Answer its questions by voice (yes / no)", "bool"),
        FieldSpec("speaker_verification", "Only my voice gives commands (enroll first: Menu)", "bool"),
        FieldSpec("speaker_threshold", "Voice match strictness", "float", low=0.1, high=0.95, step=0.05),
        FieldSpec("wake_word_model", "Custom wake-word model (.onnx)", "text", tip="Empty = built-in 'hey jarvis'"),
        FieldSpec("voice_language", "Language", "text", tip="auto = detect (among the languages below), or en, bn ..."),
        FieldSpec("voice_languages", "Languages I speak", "text", tip="With auto, e.g. en,bn for English + Bangla"),
        FieldSpec("stt_model", "Speech model", "choice", options=STT_MODELS,
                  tip="auto = large-v3-turbo on an NVIDIA GPU, else small.en on the CPU"),
        FieldSpec("stt_device", "Run speech model on", "choice", options=STT_DEVICES),
        FieldSpec("stt_compute_type", "Precision", "choice", options=STT_COMPUTE_TYPES),
        FieldSpec("stt_min_confidence", "Minimum confidence", "float", low=0, high=1, step=0.05),
        FieldSpec("stt_second_pass", "Unsure? Listen again more carefully (GPU)", "bool",
                  tip="A second, slower recognition pass before it asks you to repeat"),
        FieldSpec("early_transcription", "Start recognizing at my first pause (GPU, faster)", "bool"),
        FieldSpec("stt_bangla_model", "Bangla speech model (optional)", "text",
                  tip="A Bangla-tuned Whisper in CTranslate2 format: a folder or Hugging Face repo (restart after)"),
        FieldSpec("custom_vocabulary", "Words I use often", "text",
                  tip="Names, places, apps - comma-separated, e.g. Labib, FLUX IT, Dhanmondi. Improves recognition"),
        FieldSpec("vad_engine", "Speech detection", "choice", options=VAD_ENGINES,
                  tip="auto = neural (Silero): not fooled by fans, typing or a TV"),
        FieldSpec("silence_timeout", "Silence that ends a sentence (s)", "float", low=0.3, high=5, step=0.1),
        FieldSpec("max_record_seconds", "Longest recording (s)", "float", low=2, high=120, step=1),
    )),
    ("Voice output", (
        FieldSpec("tts_enabled", "Speak replies", "bool"),
        FieldSpec("tts_engine", "Voice", "choice", options=("jarvis",),
                  tip="The JARVIS voice: British male, calm, with a subtle AI finish (Bangla uses the Bangla voice)"),
        FieldSpec("tts_neural_voice_bn", "Neural voice (Bangla)", "text", tip="bn-BD-PradeepNeural or bn-BD-NabanitaNeural"),
        FieldSpec("tts_style", "Voice finish", "choice", options=("natural", "classic"),
                  tip="natural = sounds like a person; classic = the older synthetic 'AI' edge (restart to switch)"),
        FieldSpec("tts_speed", "Speed", "float", low=0.5, high=2, step=0.1),
        FieldSpec("tts_volume", "Volume", "float", low=0, high=1, step=0.05),
        FieldSpec("output_device", "Speakers", "choice", tip="Empty = Windows default"),
    )),
    ("Browser", (
        FieldSpec("browser_channel", "Browser to drive", "choice", options=BROWSER_CHANNELS,
                  tip="msedge is built into Windows; chromium needs 'python -m playwright install chromium'"),
        FieldSpec("browser_headless", "Run invisibly (headless)", "bool"),
        FieldSpec("browser_idle_minutes", "Close after unused (minutes)", "float", low=1, high=240, step=1),
        FieldSpec("search_provider", "Web search", "choice", options=SEARCH_PROVIDERS),
        FieldSpec("web_browser", "Open websites in", "choice", options=WEB_BROWSERS,
                  tip="default = your Windows default browser"),
        FieldSpec("chrome_profile", "Chrome profile for 'open Chrome'", "text",
                  tip="last = the profile you used last; none = show Chrome's picker; or a name, e.g. Your Chrome"),
        FieldSpec("edge_profile", "Edge profile for 'open Edge'", "text", tip="last, none, or a profile name"),
    )),
    ("Screen", (
        FieldSpec("vision_model", "Vision model", "choice",
                  tip="Empty = an installed model that can see images, e.g. 'ollama pull qwen2.5vl:3b'"),
        FieldSpec("ocr_engine", "Text recognition (OCR)", "choice", options=OCR_ENGINES,
                  tip="auto = built-in Windows OCR, then Tesseract if installed"),
        FieldSpec("ocr_language", "OCR language", "text", tip="Empty = your Windows languages. e.g. en-US, bn"),
        FieldSpec("screenshot_dir", "Save screenshots in", "folder", tip="Empty = Pictures\\Screenshots\\<name>"),
        FieldSpec("screen_blocked_windows", "Never capture windows with", "text",
                  tip="Comma-separated words matched in window titles and program names"),
    )),
    ("Privacy", (
        FieldSpec("microphone_enabled", "Allow the microphone", "bool"),
        FieldSpec("full_power", "FULL POWER: nothing is off-limits - critical actions ask me on screen", "bool",
                  tip="Formatting disks, system folders, protected processes, system tools (PowerShell, reg...), "
                      "Jarvis's own security code: instead of being refused, they ask you in a CRITICAL dialog - "
                      "never by voice, never from a phone, never pre-approved, never in automations."),
        FieldSpec("autonomy", "How much Jarvis may do by itself", "choice",
                  options=("routine", "observe", "suggest", "approval"),
                  tip="observe = only reads and answers; suggest = plans the steps but doesn't do them; "
                      "routine = low-risk actions run, risky ones ask; approval = everything that changes something asks"),
        FieldSpec("autonomy_remote", "... for requests from my phone", "choice",
                  options=("routine", "observe", "suggest", "approval"),
                  tip="The stricter of this and the level above applies to phone requests"),
        FieldSpec("autonomy_ask_categories", "Always ask for these kinds of actions", "text",
                  tip="Comma-separated tool categories, e.g. files, phone, home (smart home), apps, dev, office, system, windows, browser"),
        FieldSpec("autonomy_block_categories", "Never do these kinds of actions", "text",
                  tip="Comma-separated tool categories that are switched off (reading still works)"),
        FieldSpec("confirm_power_actions", "Ask before a restart / shutdown I request myself", "bool",
                  tip="Off: 'restart my PC' runs after a 10 s countdown (say 'cancel' or press Esc). "
                      "When the AI decides on its own, it always asks."),
        FieldSpec("save_before_power", "Save open work before a restart / shutdown", "bool",
                  tip="Presses Ctrl+S in windows whose title shows unsaved changes"),
        FieldSpec("screen_capture_enabled", "Allow screen capture (screenshots, reading the screen)", "bool"),
        FieldSpec("context_window_titles", "Understand what I'm working on from window titles (not the screen)",
                  "bool"),
        FieldSpec("browser_enabled", "Allow browser automation", "bool"),
        FieldSpec("presence_detection", "Webcam: notice when I leave / come back", "bool",
                  tip="Frames are checked for a face on this PC and never saved"),
        FieldSpec("presence_lock", "Lock the PC when I've left", "bool"),
        FieldSpec("presence_away_minutes", "Away after (minutes)", "float", low=0.5, high=60, step=0.5),
        FieldSpec("mini_orb", "Mini orb while the window is hidden", "bool"),
        FieldSpec("mini_orb_size", "Mini orb size (pixels)", "int", low=40, high=200),
        FieldSpec("remote_enabled", "Phone remote on my home network (Menu -> Phone remote)", "bool"),
        FieldSpec("remote_port", "Phone remote port", "int", low=1024, high=65535),
        FieldSpec("remote_allow_tailscale", "Phone remote also over my Tailscale network (away from home)", "bool"),
        FieldSpec("wake_public_address", "Wake from outside: router's public IP or DDNS name (optional)", "text"),
        FieldSpec("wake_public_port", "Wake from outside: UDP port the router forwards to this PC", "int", low=1,
                  high=65535),
        FieldSpec("remote_https", "Phone remote over https (talk from the phone, alerts)", "bool",
                  tip="The phone warns once about the self-signed certificate: Advanced -> Proceed"),
        FieldSpec("mcp_enabled", "Use tools from my MCP servers (config\\mcp.json)", "bool",
                  tip="Each MCP tool asks before it runs unless you set a level for it in mcp.json"),
        FieldSpec("plugins_enabled", "Load my plugins (data\\plugins, trusted code only)", "bool"),
        FieldSpec("update_manifest_url", "Update manifest URL", "text", tip="https://.../update.json; empty = off"),
        FieldSpec("update_require_signature", "Only install signed updates", "bool"),
        FieldSpec("offline_mode", "Internet", "choice", options=OFFLINE_MODES,
                  tip="auto = online, offline only while the connection is down; on = always offline"),
    )),
    ("Smart home", (
        FieldSpec("home_assistant_url", "Home Assistant address", "text", tip="e.g. http://192.168.1.10:8123; empty = off"),
        FieldSpec("home_assistant_token", "Access token", "secret",
                  tip="Home Assistant -> your profile -> Security -> Long-lived access tokens. Stays on this PC"),
    )),
    ("Google (Calendar, Drive)", (
        FieldSpec("google_client_id", "OAuth client ID", "text",
                  tip="Google Cloud Console -> APIs & Services -> Credentials -> OAuth client ID -> Desktop app. Then say 'connect Google'"),
        FieldSpec("google_client_secret", "OAuth client secret", "secret", tip="From the same client. Stays on this PC"),
    )),
    ("Developer", (
        FieldSpec("github_token", "GitHub token (read-only)", "secret",
                  tip="GitHub -> Settings -> Developer settings -> fine-grained token, read-only. Stays on this PC"),
        FieldSpec("dev_project_dir", "Default project folder", "folder"),
        FieldSpec("dev_command_timeout", "Command timeout (seconds)", "float", low=5, high=3600, step=5),
    )),
    ("Logging", (
        FieldSpec("debug", "Debug mode (verbose log)", "bool"),
        FieldSpec("log_level", "Log level", "choice", options=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
    )),
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


SECTION_ICONS = {"General": "◈", "AI": "◉", "Voice input": "◎", "Voice output": "◍", "Browser": "◇", "Screen": "▣",
                 "Privacy": "◆", "Smart home": "⌂", "Developer": "⌘", "Logging": "≡"}

DIALOG_STYLE = """
QListWidget#sections { background: rgba(4,12,24,0.9); border: 1px solid #153a57; border-radius: 4px; padding: 6px 0;
                       font-family: 'Cascadia Mono', 'Consolas', monospace; font-size: 9pt; outline: 0; }
QListWidget#sections::item { padding: 9px 14px; color: #9fc6e4; border-left: 2px solid transparent; }
QListWidget#sections::item:selected { background: rgba(56,189,248,0.10); color: #ffffff; border-left: 2px solid #38bdf8; }
QListWidget#sections::item:hover { color: #ffffff; }
QListWidget#sections::item:disabled { color: #2c4257; }
QScrollArea#page { background: transparent; border: 1px solid #153a57; border-radius: 4px; }
QWidget#pageBody { background: transparent; }
QLineEdit#search { border-radius: 14px; padding: 6px 14px; font-size: 9.5pt; }
QWidget#pageBody QLineEdit { border-radius: 4px; padding: 5px 10px; font-size: 10pt; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #1f5a80; border-radius: 3px; background: #050c16; }
QCheckBox::indicator:checked { background: #0e5d80; border-color: #38bdf8; }
"""


def matches(spec: FieldSpec, query: str) -> bool:
    """Search: every word must appear in the label, tip, setting name or .env name."""
    haystack = " ".join((spec.label, spec.tip, spec.name.replace("_", " "), ENV_NAMES.get(spec.name, ""))).lower()
    return all(word in haystack for word in query.lower().split())


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: Settings,
        env_file: Path,
        *,
        models: list[str] | None = None,
        microphones: list[str] | None = None,
        speakers: list[str] | None = None,
        on_saved: Callable[[Settings, set[str]], None] | None = None,
        on_restart: Callable[[list[str]], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(820, 560)
        self.setStyleSheet(DIALOG_STYLE)
        self.settings = settings
        self.env_file = env_file
        self.on_saved = on_saved
        self.on_restart = on_restart  # called with the names that only apply after a restart (Jarvis does it itself)
        self.needs_restart: list[str] = []
        self.saved: Settings | None = None
        self.widgets: dict[str, QWidget] = {}
        self._rows: list[tuple[int, QFormLayout, int, FieldSpec]] = []  # section, form, row, spec
        dynamic = {"ai_model": models or [], "vision_model": models or [], "ai_smart_model": models or [], "ai_coding_model": models or [], "microphone_device": microphones or [], "output_device": speakers or []}

        title = QLabel("SYSTEM CONFIGURATION")
        title.setObjectName("sectionLabel")
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search settings  (e.g. voice, bangla, token, gpu)")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search settings")
        for clear in self.search.findChildren(QToolButton):
            clear.setAccessibleName("Clear search")
        self.search.textChanged.connect(self.filter)
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.search, 2)

        self.sections = QListWidget()
        self.sections.setObjectName("sections")
        self.sections.setAccessibleName("Settings sections")
        self.sections.setFixedWidth(190)
        self.pages = QStackedWidget()
        for index, (name, specs) in enumerate(TABS):
            body = QWidget()
            body.setObjectName("pageBody")
            column = QVBoxLayout(body)
            heading = QLabel(name.upper())
            heading.setObjectName("sectionLabel")
            column.addWidget(heading)
            form = QFormLayout()
            form.setHorizontalSpacing(18)
            form.setVerticalSpacing(10)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            for spec in specs:
                widget = self._make(spec, getattr(settings, spec.name), dynamic.get(spec.name))
                label = QLabel(spec.label)
                label.setWordWrap(True)
                label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if spec.tip:
                    label.setToolTip(spec.tip)
                    widget.setToolTip(spec.tip)
                field = self.widgets[spec.name]               # the control a screen reader lands on
                field.setAccessibleName(spec.label)
                if spec.tip:
                    field.setAccessibleDescription(spec.tip)
                label.setBuddy(field)                         # and Alt+<letter> / clicking the label reaches it
                form.addRow(label, widget)
                self._rows.append((index, form, form.rowCount() - 1, spec))
            column.addLayout(form)
            column.addStretch(1)
            scroll = QScrollArea()
            scroll.setObjectName("page")
            scroll.setWidgetResizable(True)
            scroll.setWidget(body)
            self.pages.addWidget(scroll)
            self.sections.addItem(QListWidgetItem(f"{SECTION_ICONS.get(name, '·')}  {name}"))
        self.sections.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.sections.setCurrentRow(0)

        body_row = QHBoxLayout()
        body_row.addWidget(self.sections)
        body_row.addWidget(self.pages, 1)

        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addLayout(body_row, 1)
        layout.addWidget(self.note)
        layout.addWidget(buttons)
        overridden = overridden_by_environment([ENV_NAMES[n] for _, specs in TABS for n in (s.name for s in specs)])
        if overridden:
            self.note.setText("Set by environment variables (they override .env): " + ", ".join(overridden))
        self.note.setVisible(False)        # hidden at your request (the list is still in the tooltip of Save)
        if overridden:
            buttons.setToolTip(self.note.text())

    # ---------------------------------------------------------------- search

    def filter(self, query: str) -> int:
        """Show only the settings that match; sections without a match are greyed out. Returns the match count."""
        query = query.strip()
        counts = [0] * len(TABS)
        for section, form, row, spec in self._rows:
            visible = not query or matches(spec, query)
            form.setRowVisible(row, visible)
            counts[section] += visible
        for index, (name, _) in enumerate(TABS):
            item = self.sections.item(index)
            label = f"{SECTION_ICONS.get(name, '·')}  {name}"
            item.setText(f"{label}  ({counts[index]})" if query and counts[index] else label)
            flags = item.flags()
            enabled = bool(counts[index]) or not query
            item.setFlags(flags | Qt.ItemFlag.ItemIsEnabled if enabled else flags & ~Qt.ItemFlag.ItemIsEnabled)
        if query and counts[self.sections.currentRow()] == 0:
            first = next((i for i, c in enumerate(counts) if c), None)
            if first is not None:
                self.sections.setCurrentRow(first)
        return sum(counts)

    # --------------------------------------------------------------- widgets

    def _make(self, spec: FieldSpec, value: Any, dynamic: list[str] | None) -> QWidget:
        if spec.kind == "bool":
            widget = QCheckBox()
            widget.setChecked(bool(value))
        elif spec.kind == "choice":
            widget = QComboBox()
            options = list(spec.options) or [""] + list(dynamic or [])
            current = _text(value)
            if current not in options:
                options.insert(0 if not spec.options else len(options), current)
            widget.addItems(options)
            widget.setCurrentText(current)
        elif spec.kind == "float":
            widget = QDoubleSpinBox()
            widget.setRange(spec.low, spec.high)
            widget.setSingleStep(spec.step)
            widget.setDecimals(2 if spec.step < 1 else 0)
            widget.setValue(float(value))
        elif spec.kind == "int":
            widget = QSpinBox()
            widget.setRange(int(spec.low), int(spec.high))
            widget.setSingleStep(int(spec.step))
            widget.setValue(int(value))
        elif spec.kind == "folder":
            edit = QLineEdit(_text(value))
            browse = QPushButton("Browse...")
            browse.clicked.connect(lambda: self._browse(edit))
            box = QWidget()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(edit, 1)
            row.addWidget(browse)
            self.widgets[spec.name] = edit
            return box
        else:
            widget = QLineEdit(_text(value))
            if spec.kind == "secret":
                widget.setEchoMode(QLineEdit.EchoMode.Password)
                widget.setPlaceholderText("not set")
        self.widgets[spec.name] = widget
        return widget

    def _browse(self, edit: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose the project folder", edit.text())
        if folder:
            edit.setText(str(Path(folder)))

    def value_of(self, name: str) -> str:
        widget = self.widgets[name]
        if isinstance(widget, QCheckBox):
            return "true" if widget.isChecked() else "false"
        if isinstance(widget, QComboBox):
            return widget.currentText().strip()
        if isinstance(widget, QSpinBox):
            return str(widget.value())
        if isinstance(widget, QDoubleSpinBox):
            return f"{widget.value():g}"
        return widget.text().strip()  # type: ignore[union-attr]

    def changes(self) -> dict[str, str]:
        """ENV_NAME -> new text, only for fields that changed."""
        result = {}
        for name in self.widgets:
            new = self.value_of(name)
            old = _text(getattr(self.settings, name))
            if isinstance(getattr(self.settings, name), float):
                try:
                    same = float(new) == float(old)
                except ValueError:
                    same = False
            else:
                same = new == old
            if not same:
                result[ENV_NAMES[name]] = new
        return result

    # ------------------------------------------------------------------ save

    def save(self) -> None:
        changes = self.changes()
        if not changes:
            self.accept()
            return
        try:
            self.saved = update_env_file(self.env_file, changes)
        except (ConfigError, OSError) as exc:
            QMessageBox.warning(self, "Settings not saved", str(exc))
            return
        changed_fields = {name for name, env in ENV_NAMES.items() if env in changes}
        if self.on_saved is not None:
            self.on_saved(self.saved, changed_fields)
        self.needs_restart = sorted(changed_fields - LIVE_FIELDS)
        self.accept()
        if self.needs_restart and self.on_restart is not None:   # applied for you: Jarvis restarts itself
            self.on_restart([ENV_NAMES[n] for n in self.needs_restart])
