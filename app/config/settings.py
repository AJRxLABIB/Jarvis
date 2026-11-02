"""Environment settings loaded from ``.env`` and process environment variables.

Precedence (highest first): real environment variables, ``.env`` file, defaults.
Settings are immutable; to change them, write the new values and reload.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SUPPORTED_AI_PROVIDERS: tuple[str, ...] = ("ollama",)
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_SENSITIVE_FIELD_MARKERS = ("key", "token", "secret", "password")

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _\-]{0,31}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/\-]{0,128}$")
_WAKE_RE = re.compile(r"^[a-z]+( [a-z]+){0,3}$")
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")
_ADDRESS_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{0,23}$")
_PIPER_VOICE_RE = re.compile(r"^[a-z]{2,3}_[A-Z]{2}-[A-Za-z0-9_]+-(x_low|low|medium|high)$")
_DEVICE_RE = re.compile(r"^[^\x00-\x1f]{0,128}$")
STT_MODELS: tuple[str, ...] = (
    "auto", "tiny.en", "tiny", "base.en", "base", "small.en", "small", "medium.en", "medium",
    "large-v2", "large-v3", "large-v3-turbo", "turbo", "distil-small.en", "distil-medium.en", "distil-large-v3",
)
STT_DEVICES: tuple[str, ...] = ("cpu", "cuda", "auto")
STT_COMPUTE_TYPES: tuple[str, ...] = ("auto", "default", "int8", "int8_float16", "int8_float32", "float16", "float32")
TTS_ENGINES: tuple[str, ...] = ("jarvis", "auto", "neural", "kokoro", "piper", "sapi")  # jarvis = the voice profile
WEB_BROWSERS: tuple[str, ...] = ("default", "chrome", "edge", "firefox")
OFFLINE_MODES: tuple[str, ...] = ("auto", "on", "off")
WAKE_WORD_ENGINES: tuple[str, ...] = ("auto", "openwakeword", "whisper")
VAD_ENGINES: tuple[str, ...] = ("auto", "silero", "energy")
BROWSER_CHANNELS: tuple[str, ...] = ("msedge", "chrome", "chromium")
SEARCH_PROVIDERS: tuple[str, ...] = ("duckduckgo",)
OCR_ENGINES: tuple[str, ...] = ("auto", "windows", "tesseract")
_OCR_LANG_RE = re.compile(r"^([A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2}|[a-z_]{3,20}(\+[a-z_]{3,20}){0,3})?$")
DEFAULT_BLOCKED_WINDOWS = ("password, keepass, 1password, bitwarden, lastpass, dashlane, nordpass, proton pass, "
                           "credential manager, authenticator, inprivate")
_KEEP_ALIVE_RE = re.compile(r"^-?\d+(\.\d+)?(ms|s|m|h)?$")


class ConfigError(Exception):
    """A configuration problem, with a message that can be shown to the user."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    assistant_name: str = "Jarvis"
    user_address: str = ""  # e.g. "sir" or a name; empty = no honorific
    user_name: str = Field(default="", max_length=40)  # your name, shown on the start screen (empty = "THIS PC")
    ai_provider: str = "ollama"
    ollama_host: str = "http://127.0.0.1:11434"
    ai_model: str = ""  # empty = choose from installed Ollama models
    auto_start_ollama: bool = True
    hide_ollama_window: bool = True  # at start-up, minimise the window the Ollama app opens (it keeps running)
    # false: "restart / shut down my PC" said or typed by you runs without the dialog (after a 10 s countdown
    # you can cancel). When the AI decides on its own, it always asks.
    confirm_power_actions: bool = True
    save_before_power: bool = True  # press Ctrl+S in windows with unsaved changes before a restart/shutdown  # start the installed Ollama app if Jarvis can't reach it
    ai_request_timeout: float = Field(default=120.0, gt=0, le=3600)
    ai_temperature: float = Field(default=0.3, ge=0, le=2)
    ai_context_window: int = Field(default=8192, ge=1024, le=262144)
    ai_fast_model: str = ""  # optional small model for quick chat/commands; AI_MODEL answers the harder questions
    ai_smart_model: str = ""  # optional bigger model for hard questions (explain, write, compare, plan ...)
    ai_coding_model: str = ""  # optional coding model (e.g. qwen2.5-coder:7b) for code, errors, tests, builds
    auto_sort_downloads: bool = False  # sort new files in Downloads into folders by type (undo: "undo organize")
    embedding_model: str = "nomic-embed-text"  # local embeddings for search by meaning (ollama pull nomic-embed-text)
    conversation_memory: bool = False  # remember our conversations (on this PC) and continue after a restart
    memory_suggestions: bool = True  # offer lasting facts you mention as memories; nothing is saved until you accept
    tone_aware: bool = True  # sound rushed or tired -> shorter, gentler answers (speed and loudness only)
    document_folders: str = Field(default="", max_length=2000)  # ;-separated folders for "ask my documents"
    clipboard_history: bool = False  # remember what you copy (text only; secrets and card numbers are never kept)
    clipboard_retention_days: int = Field(default=7, ge=1, le=90)
    ai_auto_smart: bool = True  # hard questions use a bigger installed model when it fits entirely on the GPU (app/ai/thinking_swap.py)
    ai_auto_switch: bool = False  # switch to a benchmark-backed model when the evidence says so (app/ai/switching.py)
    core_renderer: Literal["auto", "gl", "painter"] = "auto"   # the 3D core: auto = OpenGL when possible, else painter
    visual_quality: Literal["auto", "low", "balanced", "high"] = "auto"
    core_speed: float = Field(default=1.0, ge=0.3, le=2.0)       # core animation speed
    glow_intensity: float = Field(default=1.0, ge=0.3, le=2.0)
    reduced_motion: bool = False                                  # calm motion: slower core, no sway or breathing
    core_hud_rings: bool = False                                  # the flat rings / gauges / tick bezel around the core
    core_pulses: bool = True                                      # data pulses from the core to the panel that changed
    core_trails: bool = True                                      # sparks with fading trails circling the core
    ui_sounds: bool = False                                       # short interface sounds (tick / attention / done / error)
    ui_sound_volume: float = Field(default=0.5, ge=0.0, le=1.0)
    ui_theme: Literal["dark", "light", "forest"] = "dark"                   # the command-center view (the HUD is always dark)
    ui_text_scale: float = Field(default=1.0, ge=0.8, le=1.6)     # text size in the command-center view
    hud_scale: float = Field(default=1.0, ge=0.75, le=1.5)        # the whole window's size (applies at the next start)
    hidden_panels: str = ""                                       # dashboard panels to hide, comma separated titles
    taint_lockdown: bool = False  # after reading web/email/documents, the model is not even offered send/save tools
    ai_think: bool = False  # let "thinking" models reason first (slower)
    ollama_keep_alive: str = "30m"
    context_max_messages: int = Field(default=24, ge=2, le=200)
    context_max_chars: int = Field(default=16000, ge=1000, le=500000)
    wake_word: str = "hey jarvis"
    hands_free: bool = False  # always listen for the wake phrase (microphone stays open)
    wake_word_engine: str = "auto"  # auto | openwakeword | whisper
    wake_word_threshold: float = Field(default=0.5, ge=0.1, le=0.95)  # lower = more sensitive
    wake_on_name: bool = True  # also wake on just the name ("Jarvis", "Jarvis, are you there?")
    fcm_service_account: Path | None = None  # Firebase service-account JSON for phone push wake-ups (your own project)
    wake_word_model: Path | None = None  # custom openWakeWord .onnx for another wake phrase
    # once woken, keep the conversation going without the wake phrase until this long without a request
    follow_up_seconds: float = Field(default=20.0, ge=0, le=900)
    wake_sound: bool = True  # short chime when the wake phrase is heard
    voice_language: str = "en"  # en, bn, ... or auto (detect, limited to VOICE_LANGUAGES)
    voice_languages: str = Field(default="en,bn", max_length=40)  # with auto: the languages you speak
    tts_enabled: bool = True
    tts_engine: str = "jarvis"  # the JARVIS voice (British neural voice + finish; offline: Kokoro / Windows)
    tts_voice: str = "en_US-lessac-medium"  # Piper voice id
    # TTS_ENGINE=neural: natural online voices (text is sent to Microsoft's speech service), offline fallback
    tts_neural_voice_en: str = Field(default="en-GB-RyanNeural", max_length=60)
    tts_neural_voice_bn: str = Field(default="bn-BD-PradeepNeural", max_length=60)
    tts_kokoro_voice: str = Field(default="bm_george", max_length=40)  # offline English voice (TTS_ENGINE=kokoro)
    tts_sapi_voice: str = ""  # part of a Windows voice name, e.g. "Zira"; empty = default
    tts_style: Literal["natural", "classic"] = "natural"   # natural = human, professional; classic = the old robotic edge
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    tts_volume: float = Field(default=1.0, ge=0.0, le=1.0)
    microphone_enabled: bool = True
    microphone_device: str = ""  # index or part of the name; empty = Windows default
    output_device: str = ""
    # auto = large-v3-turbo on an NVIDIA GPU, else small.en on the CPU (with fallbacks)
    stt_model: str = "auto"
    stt_device: str = "auto"
    stt_compute_type: str = "auto"
    stt_beam_size: int = Field(default=5, ge=1, le=10)
    custom_vocabulary: str = Field(default="", max_length=1500)  # names/words you use often, comma-separated
    stt_second_pass: bool = True  # unsure of a sentence: listen again more carefully (GPU) before asking you
    early_transcription: bool = True  # GPU: start recognizing at your first pause, so the answer comes sooner
    stt_bangla_model: str = Field(default="", max_length=200)  # Bangla-tuned Whisper (CTranslate2): folder or HF repo
    personal_wake_check: bool = True  # after Menu -> Teach me my wake word: your recordings confirm each wake-up
    stt_min_confidence: float = Field(default=0.35, ge=0.0, le=1.0)
    vad_engine: str = "auto"  # auto = neural (Silero) speech detection if available, else energy
    silence_timeout: float = Field(default=0.7, ge=0.3, le=5.0)   # unfinished sentences still get a grace period
    speech_start_timeout: float = Field(default=6.0, ge=1.0, le=30.0)
    max_record_seconds: float = Field(default=15.0, ge=2.0, le=120.0)
    offline_mode: str = "auto"  # auto = online, offline only while there's no internet | on | off
    debug: bool = False
    log_level: str = "INFO"
    log_max_bytes: int = Field(default=5_000_000, ge=100_000, le=500_000_000)
    log_backup_count: int = Field(default=5, ge=1, le=50)
    data_dir: Path | None = None
    dev_project_dir: Path | None = None  # default folder for git/test/dev commands
    jarvis_source_dir: Path | None = None  # Jarvis's own source (for the evolution engine); empty = auto when run from source
    mini_orb_size: int = Field(default=64, ge=40, le=200)  # pixels
    mini_orb: bool = True  # small always-on-top orb in the screen corner while the window is hidden
    presence_detection: bool = False  # opt-in: the webcam notices when you sit down / walk away
    gesture_control: bool = False  # opt-in: allow hand-gesture mouse control (the camera still opens only when you turn it on)
    presence_lock: bool = True  # with presence detection: lock the PC when you've been away
    presence_away_minutes: float = Field(default=3.0, ge=0.5, le=60)
    setup_done: bool = False  # the first-run setup wizard has been completed
    plugins_enabled: bool = False  # load your own tool plugins from the data folder's "plugins" (trusted code only)
    mcp_enabled: bool = True  # tools from the MCP servers listed in config\\mcp.json (none until you add some)
    update_manifest_url: str = Field(default="", max_length=400)  # https URL of an update manifest (empty = off)
    update_require_signature: bool = False  # only install updates with a valid Windows code signature
    settings_version: int = Field(default=0, ge=0, le=1000)  # upgraded automatically
    start_minimized: bool = False  # start in the system tray
    start_with_windows: bool = False  # start (in the tray) when you sign in to Windows
    direct_commands: bool = True  # "open X" / "close X" for known apps skip the AI model
    browser_enabled: bool = True  # allow the assistant's own automated browser
    browser_channel: str = "msedge"  # installed browser to drive: msedge, chrome, or chromium (bundled)
    browser_headless: bool = False  # true = run the browser invisibly
    browser_idle_minutes: float = Field(default=10.0, ge=1, le=240)  # close it after this long unused
    search_provider: str = "duckduckgo"
    web_browser: str = "default"  # default (the Windows default browser) | chrome | edge | firefox
    chrome_profile: str = Field(default="last", max_length=80)  # last | none | a profile name, e.g. "Your Chrome"
    edge_profile: str = Field(default="last", max_length=80)
    context_window_titles: bool = True  # context engine: read the foreground window's title (never the screen)
    screen_capture_enabled: bool = False  # opt-in: allow screenshots, reading text on screen, describing it
    speech_dataset: Literal["off", "corrections", "all"] = "off"  # keep your voice commands to train the recognizer
    screenshot_dir: Path | None = None  # empty = your Downloads folder
    screen_blocked_windows: str = Field(default=DEFAULT_BLOCKED_WINDOWS, max_length=1000)
    ocr_engine: str = "auto"  # auto | windows | tesseract
    ocr_language: str = ""  # empty = your Windows languages; e.g. en-US, bn
    vision_model: str = ""  # Ollama model for describing the screen; empty = pick an installed vision model
    home_location: str = Field(default="", max_length=80)  # your city, for "what's the weather" and the briefing
    temperature_unit: str = "celsius"  # celsius | fahrenheit
    music_default_query: str = Field(default="top hits playlist", max_length=80)  # "play some music"
    news_region: str = Field(default="BD", min_length=2, max_length=2)  # country code for the headlines
    proactive_alerts: bool = True  # speak up on its own: low battery, charger plugged in/out
    pc_watcher: bool = True  # warn about a full disk, an app hogging the CPU, a hot GPU, finished downloads
    low_battery_percent: int = Field(default=20, ge=5, le=50)
    proactive_level: Literal["off", "minimal", "normal", "active", "high"] = "normal"  # how readily Jarvis speaks up
    quiet_hours: str = "23:00-07:00"  # suggestions only go to the notification center then; empty = none
    briefing_length: Literal["short", "normal", "full"] = "normal"   # "good morning" / brief me
    briefing_sections: str = Field(default="", max_length=200)      # empty = all; e.g. "reminders,todos,weather"
    automation_quiet_hours: bool = False  # event-started automations (files, battery, Wi-Fi...) wait out quiet hours
    proactive_max_per_hour: int = Field(default=4, ge=1, le=30)
    proactive_categories: str = ""  # comma list (goal, mission, project, system); empty = all
    proactive_emergency_exceptions: bool = True  # urgent security / battery alerts even when proactive is off
    proactive_suggestions: bool = True  # "you usually open VS Code around now - shall I open it?"
    idle_unload_minutes: float = Field(default=20, ge=0, le=720)  # free the AI model's memory when unused (0 = never)
    focus_apps: str = Field(default="Steam, Epic Games Launcher, Discord, WhatsApp, Telegram, Messenger", max_length=500)
    intro_animation: bool = True  # the full-screen intro when Jarvis starts
    step_aside: bool = True  # after opening / searching something, fade into the mini orb so you see the app
    world_context: bool = True  # give the AI the few facts about your projects/goals/mission a request needs
    reuse_tabs: bool = True  # songs / YouTube reuse the same browser tab; other sites open a new tab
    always_on_top: bool = False  # keep the Jarvis window above every other window ("stay on top")
    start_fullscreen: bool = True  # the command center opens full screen (F11 / "exit your full screen")
    animations: bool = True  # HUD motion: panels drawing in, gliding meters, the background scan
    weekly_health_check: bool = True  # once a week while you're away: health report; speaks only if worse
    ollama_fast_memory: bool = True  # Ollama started by Jarvis: flash attention + 8-bit KV cache (fits small GPUs)
    gpu_preference: Literal["auto", "integrated", "nvidia"] = "auto"   # laptops with two GPUs: integrated = keep JARVIS off the NVIDIA card (app/utils/gpu_pref.py)
    gpu_memory_saver: bool = True  # small GPU (<= 8 GB) with speech on it: smaller AI context so both fit
    home_assistant_url: str = Field(default="", max_length=200)  # e.g. http://192.168.1.10:8123
    home_assistant_token: str = Field(default="", max_length=400, repr=False)  # long-lived access token (secret)
    remote_enabled: bool = False  # phone remote: a private web page on your home network
    remote_port: int = Field(default=8765, ge=1024, le=65535)
    local_discovery: bool = False  # announce this PC on the local network (mDNS) so other JARVIS devices can find it; finding is never trusting
    wake_public_address: str = Field(default="", max_length=253)  # router's public IP / DDNS name, for waking from outside
    wake_public_port: int = Field(default=9, ge=1, le=65535)  # the UDP port your router forwards to this PC
    remote_allow_tailscale: bool = False  # also answer phones on your own Tailscale network (away from home)
    remote_https: bool = True  # https (self-signed): lets you talk from the phone and get its alerts
    barge_in: bool = True  # "Hey Jarvis" while it's speaking stops the speech and listens
    voice_interrupt: bool = True  # talk over it ("stop", or a new command) while it speaks (needs a GPU)
    voice_confirm: bool = True  # answer "yes"/"no" (হ্যাঁ/না) out loud to its questions
    performance_mode: Literal["auto", "fast", "balanced", "quality", "battery"] = "balanced"   # app/core/perf.py (measured limits)
    autonomy: Literal["observe", "suggest", "routine", "approval"] = "routine"   # how much Jarvis may do by itself
    full_power: bool = False   # nothing is off-limits: CRITICAL actions ask you ON SCREEN instead of being blocked
    autonomy_remote: Literal["observe", "suggest", "routine", "approval"] = "routine"  # requests from a phone
    autonomy_ask_categories: str = Field(default="", max_length=300)    # tool categories that always ask
    autonomy_block_categories: str = Field(default="", max_length=300)  # tool categories switched off
    speaker_verification: bool = False  # only the enrolled voice can give commands (Menu -> Enroll my voice)
    speaker_threshold: float = Field(default=0.45, ge=0.1, le=0.95)  # higher = stricter
    speak_typed_replies: bool = False  # also speak answers to typed messages
    show_sources: bool = True  # under answers that used tools / saved memories: where the facts came from
    learn_from_use: bool = True  # learn skills & lessons from every request's real outcome (app/agent/growth.py)
    monitor_interval: float = Field(default=3.0, ge=1.0, le=60.0)  # seconds between system-monitor updates
    dev_command_timeout: float = Field(default=120.0, ge=5, le=3600)
    auto_backup: bool = True  # a verified copy of the database once a day (data\\backups, newest 7 kept)
    memory_event_retention_days: int = Field(default=0, ge=0, le=3650)  # forget what Jarvis recorded itself after N days (0 = keep)
    metrics_retention_days: int = Field(default=14, ge=1, le=90)  # how long health readings are kept
    github_token: str = Field(default="", max_length=400, repr=False)  # read-only GitHub access token (secret)
    google_client_id: str = Field(default="", max_length=200)  # your own Google OAuth "Desktop app" client (Calendar / Drive)
    google_client_secret: str = Field(default="", max_length=200, repr=False)  # its secret (secret setting)
    cyber_guardian: bool = False  # OFF by default: watch for unusual file bursts, startup changes, new listeners and stale backups (names/times only; never stops anything)
    guardian_folders: str = ""  # folders the guardian watches, separated by ';' (empty = Documents, Desktop, Pictures)
    process_history: bool = False  # OFF by default: also record the top programs' names with CPU / memory numbers (for per-program baselines)
    metrics_history: bool = True  # a light record of CPU / memory / disk / battery (once a minute, 14 days) for trends

    @field_validator("assistant_name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError("must start with a letter; letters, digits, spaces, '-' or '_' only (max 32)")
        return value

    @field_validator("user_address")
    @classmethod
    def _check_address(cls, value: str) -> str:
        if value and not _ADDRESS_RE.match(value):
            raise ValueError("must be a short name or title such as 'sir' or 'Labib' (max 24 letters)")
        return value

    @field_validator("ollama_keep_alive")
    @classmethod
    def _check_keep_alive(cls, value: str) -> str:
        if not _KEEP_ALIVE_RE.match(value):
            raise ValueError("must be a duration such as '10m', '1h', '0' (unload) or '-1' (keep loaded)")
        return value

    @field_validator("ai_provider")
    @classmethod
    def _check_provider(cls, value: str) -> str:
        value = value.lower()
        if value not in SUPPORTED_AI_PROVIDERS:
            raise ValueError(f"unsupported provider '{value}' (supported: {', '.join(SUPPORTED_AI_PROVIDERS)})")
        return value

    @field_validator("ollama_host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("must look like http://127.0.0.1:11434")
        if parsed.username or parsed.password:
            raise ValueError("must not contain credentials")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("must be a base URL without a path")
        return value

    @field_validator("web_browser")
    @classmethod
    def _check_web_browser(cls, value: str) -> str:
        value = value.lower()
        if value not in WEB_BROWSERS:
            raise ValueError(f"must be one of {', '.join(WEB_BROWSERS)}")
        return value

    @field_validator("offline_mode", mode="before")
    @classmethod
    def _check_offline(cls, value: Any) -> str:
        text = str(value).strip().lower() if value is not None else "auto"
        text = {"true": "on", "1": "on", "yes": "on", "false": "auto", "0": "auto", "no": "auto", "": "auto"}.get(text, text)
        if text not in OFFLINE_MODES:
            raise ValueError("must be auto (online unless the internet is down), on (always offline) or off")
        return text

    @property
    def forced_offline(self) -> bool:
        return self.offline_mode == "on"

    @field_validator("wake_word_engine")
    @classmethod
    def _check_wake_engine(cls, value: str) -> str:
        value = value.lower()
        if value not in WAKE_WORD_ENGINES:
            raise ValueError(f"must be one of {', '.join(WAKE_WORD_ENGINES)}")
        return value

    @field_validator("fcm_service_account")
    @classmethod
    def _check_fcm_file(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.is_absolute() or value.suffix.lower() != ".json"):
            raise ValueError("must be the absolute path of the Firebase service-account .json file")
        return value

    @field_validator("wake_word_model")
    @classmethod
    def _check_wake_model(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.is_absolute() or value.suffix.lower() != ".onnx"):
            raise ValueError("must be the absolute path of an openWakeWord .onnx model")
        return value

    @field_validator("ocr_engine")
    @classmethod
    def _check_ocr_engine(cls, value: str) -> str:
        value = value.lower()
        if value not in OCR_ENGINES:
            raise ValueError(f"must be one of {', '.join(OCR_ENGINES)}")
        return value

    @field_validator("ocr_language")
    @classmethod
    def _check_ocr_language(cls, value: str) -> str:
        if not _OCR_LANG_RE.match(value):
            raise ValueError("must be a language tag such as 'en-US' or 'bn', or a Tesseract code such as 'eng'")
        return value

    @field_validator("screen_blocked_windows")
    @classmethod
    def _check_blocked(cls, value: str) -> str:
        if any(ord(c) < 32 for c in value):
            raise ValueError("must be a comma-separated list of words")
        return value

    @field_validator("ai_model", "vision_model", "ai_smart_model", "ai_fast_model", "ai_coding_model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        if not _MODEL_RE.match(value):
            raise ValueError("contains characters that are not valid in an Ollama model name")
        return value

    @field_validator("wake_word")
    @classmethod
    def _check_wake_word(cls, value: str) -> str:
        value = " ".join(value.lower().split())
        if not (2 <= len(value) <= 40) or not _WAKE_RE.match(value):
            raise ValueError("must be 1-4 words of letters only, e.g. 'hey jarvis'")
        return value

    @field_validator("voice_language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        value = value.lower()
        if value != "auto" and not _LANG_RE.match(value):
            raise ValueError("must be a language code such as 'en', 'bn' or 'hi', or 'auto'")
        return value

    @field_validator("voice_languages")
    @classmethod
    def _check_languages(cls, value: str) -> str:
        codes = [c.strip().lower() for c in value.replace(";", ",").split(",") if c.strip()]
        if not codes or not all(_LANG_RE.match(c) for c in codes):
            raise ValueError("must be language codes separated by commas, e.g. 'en,bn'")
        return ",".join(dict.fromkeys(codes))

    @property
    def allowed_languages(self) -> tuple[str, ...]:
        """Languages speech recognition may choose from when VOICE_LANGUAGE=auto."""
        return tuple(self.voice_languages.split(",")) if self.voice_language == "auto" else (self.voice_language,)

    @field_validator("browser_channel")
    @classmethod
    def _check_channel(cls, value: str) -> str:
        value = value.lower()
        if value not in BROWSER_CHANNELS:
            raise ValueError(f"must be one of {', '.join(BROWSER_CHANNELS)}")
        return value

    @field_validator("search_provider")
    @classmethod
    def _check_search(cls, value: str) -> str:
        value = value.lower()
        if value not in SEARCH_PROVIDERS:
            raise ValueError(f"must be one of {', '.join(SEARCH_PROVIDERS)}")
        return value

    @field_validator("tts_engine")
    @classmethod
    def _check_tts_engine(cls, value: str) -> str:
        value = value.lower()
        if value not in TTS_ENGINES:
            raise ValueError(f"must be one of {', '.join(TTS_ENGINES)}")
        return value

    @field_validator("tts_voice")
    @classmethod
    def _check_tts_voice(cls, value: str) -> str:
        if not _PIPER_VOICE_RE.match(value):
            raise ValueError("must be a Piper voice id such as 'en_US-lessac-medium'")
        return value

    @field_validator("tts_sapi_voice", "microphone_device", "output_device", "chrome_profile", "edge_profile", "home_location")
    @classmethod
    def _check_device_text(cls, value: str) -> str:
        if not _DEVICE_RE.match(value):
            raise ValueError("must be at most 128 printable characters")
        return value

    @field_validator("stt_model")
    @classmethod
    def _check_stt_model(cls, value: str) -> str:
        if value in STT_MODELS:
            return value
        path = Path(os.path.expandvars(value))
        if path.is_absolute():
            return str(path)  # a local CTranslate2 model folder
        raise ValueError(f"must be one of {', '.join(STT_MODELS)} or an absolute path to a model folder")

    @field_validator("temperature_unit")
    @classmethod
    def _check_unit(cls, value: str) -> str:
        value = value.lower().strip()
        if value in ("c", "celsius", "metric"):
            return "celsius"
        if value in ("f", "fahrenheit", "imperial"):
            return "fahrenheit"
        raise ValueError("must be celsius or fahrenheit")

    @field_validator("vad_engine")
    @classmethod
    def _check_vad(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in VAD_ENGINES:
            raise ValueError(f"must be one of {', '.join(VAD_ENGINES)}")
        return value

    @field_validator("stt_device")
    @classmethod
    def _check_stt_device(cls, value: str) -> str:
        value = value.lower()
        if value not in STT_DEVICES:
            raise ValueError(f"must be one of {', '.join(STT_DEVICES)}")
        return value

    @field_validator("stt_compute_type")
    @classmethod
    def _check_compute(cls, value: str) -> str:
        value = value.lower()
        if value not in STT_COMPUTE_TYPES:
            raise ValueError(f"must be one of {', '.join(STT_COMPUTE_TYPES)}")
        return value

    @model_validator(mode="after")
    def _check_language_model(self) -> "Settings":
        if self.voice_language not in ("en", "auto") and self.stt_model != "auto" and self.stt_model.endswith(".en"):
            raise ValueError(
                f"STT_MODEL={self.stt_model} only understands English; "
                f"use a multilingual model such as 'small' for VOICE_LANGUAGE={self.voice_language}"
            )
        return self

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in LOG_LEVELS:
            raise ValueError(f"must be one of {', '.join(LOG_LEVELS)}")
        return value

    @field_validator("data_dir", "dev_project_dir", "screenshot_dir", "wake_word_model", "jarvis_source_dir", "fcm_service_account",
                     mode="before")
    @classmethod
    def _empty_data_dir(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return Path(os.path.expandvars(os.path.expanduser(str(value).strip())))

    @field_validator("data_dir", "dev_project_dir", "screenshot_dir", "jarvis_source_dir")
    @classmethod
    def _absolute_data_dir(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("must be an absolute path, e.g. D:\\JarvisData")
        return value

    @property
    def effective_log_level(self) -> str:
        return "DEBUG" if self.debug else self.log_level

    def public_dict(self) -> dict[str, Any]:
        """Settings safe to log or display (secret-like fields masked)."""
        data = self.model_dump(mode="json")
        for name, value in data.items():
            if value and any(marker in name for marker in _SENSITIVE_FIELD_MARKERS):
                data[name] = "[REDACTED]"
        return data


# Field name -> environment variable name.
ENV_NAMES: dict[str, str] = {name: name.upper() for name in Settings.model_fields}
ENV_NAMES["data_dir"] = "JARVIS_DATA_DIR"


def format_validation_error(error: ValidationError, names: Mapping[str, str] | None = None) -> str:
    lines = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "(root)"
        if names and location in names:
            location = names[location]
        message = str(issue["msg"]).removeprefix("Value error, ")
        lines.append(f"  - {location}: {message}")
    return "\n".join(lines)


def _read_env_file(env_file: Path) -> dict[str, str]:
    """Parse .env strictly: a line that cannot be read is an error, never silently skipped."""
    try:
        text = env_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Could not read {env_file}: {exc}") from exc
    bad = [b.original for b in parse_stream(io.StringIO(text)) if b.error]
    if bad:
        from app.utils.redaction import redact

        # dotenv reports where the chunk starts, which may be blank lines before the bad text
        shown = "\n".join(
            f"  line {o.line + o.string[: len(o.string) - len(o.string.lstrip())].count(chr(10))}: "
            f"{redact(o.string.strip())[:80]}"
            for o in bad
        )
        raise ConfigError(
            f"{env_file.name} has line(s) that are not in NAME=value form:\n{shown}\n"
            "Each setting must look like MICROPHONE_DEVICE=Microphone Array (comments start with #)."
        )
    return {k: v for k, v in dotenv_values(stream=io.StringIO(text)).items() if v is not None}


SECRET_FIELDS = tuple(name for name, info in Settings.model_fields.items()
                      if any(m in name for m in _SENSITIVE_FIELD_MARKERS) and info.annotation is str)


def load_settings(env_file: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings`. Raises :class:`ConfigError` on invalid values."""
    file_values: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        file_values = _read_env_file(env_file)

    env = os.environ if environ is None else environ
    raw: dict[str, str] = {}
    for field, env_name in ENV_NAMES.items():
        if env_name in env:
            raw[field] = env[env_name]
        elif env_name in file_values:
            raw[field] = file_values[env_name]

    for field in SECRET_FIELDS:
        if raw.get(field, "").startswith("dpapi:"):
            from app.security.protect import ProtectError, reveal_text

            try:
                raw[field] = reveal_text(raw[field])
            except ProtectError as exc:
                raise ConfigError(f"{ENV_NAMES[field]}: {exc} Enter it again in Settings.") from None
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        source = f" (from {env_file.name} or environment)" if env_file else ""
        raise ConfigError(f"Invalid settings{source}:\n{format_validation_error(exc, ENV_NAMES)}") from None
