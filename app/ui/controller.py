"""Connects the GUI to the assistant.

Threads: the Qt GUI thread; an asyncio loop thread (AI conversation, tools);
a worker thread per voice recording; the TTS worker. Every callback from a
non-GUI thread becomes a Qt signal, which Qt delivers on the GUI thread.
"""

from __future__ import annotations

import concurrent.futures
import threading
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from app.agent.conversation import AssistantReply, ConversationManager
from app.ai.base import ToolCall
from app.ai.manager import AIManager, AIStatus
from app.cli import create_tool_executor, describe_execution
from app.config import live_fields
from app.memory.conversations import ConversationStore
from app.memory.settings_store import SettingsStore
from app.security.confirmations import ConfirmationRequest
from app.tts.manager import TTSManager
from app.tts.text import SentenceSplitter
from app.config.live_fields import REMOTE_FIELDS
from app.utils.async_runner import AsyncRunner
from app.utils.logging_setup import get_logger, log_event
from app.voice.microphone import Microphone
from app.voice.pipeline import VoicePipeline
from app.voice.speech_to_text import SpeechToText
from app.voice.vad import create_vad

if TYPE_CHECKING:
    from app.main import AppContext

_log = get_logger("ui.controller")
HANDS_FREE_FIELDS = frozenset({"speaker_verification", "speaker_threshold", "hands_free", "wake_word", "wake_word_engine", "wake_word_threshold", "wake_word_model",
                               "wake_on_name",
                               "follow_up_seconds", "wake_sound", "microphone_enabled", "early_transcription",
                               "personal_wake_check"})
SCREEN_FIELDS = frozenset({"screen_capture_enabled", "screenshot_dir", "screen_blocked_windows", "ocr_engine",
                           "ocr_language", "vision_model"})


@dataclass
class PendingConfirmation:
    request: ConfirmationRequest
    future: concurrent.futures.Future
    heard: str | None


class GuiConfirmer:
    """Hands confirmation requests to the GUI and waits for the user's click."""

    def __init__(self, controller: "AssistantController") -> None:
        self._controller = controller

    async def confirm(self, request: ConfirmationRequest) -> bool:
        import asyncio

        future: concurrent.futures.Future = concurrent.futures.Future()
        before = self._controller.machine.state
        self._controller.machine.to("WAITING_FOR_PERMISSION", request.tool)
        self._controller._pending_confirmation = PendingConfirmation(request, future, self._controller._last_heard)
        self._controller.confirmation_requested.emit(self._controller._pending_confirmation)
        self._controller.ask_by_voice(self._controller._pending_confirmation)
        try:
            return bool(await asyncio.wrap_future(future))
        finally:
            self._controller._pending_confirmation = None
            if self._controller.machine.state.value == "WAITING_FOR_PERMISSION":
                self._controller.machine.to("EXECUTING" if before.value in ("EXECUTING", "THINKING") else "THINKING",
                                            "answered")
            if self._controller.hands_free is not None:
                self._controller.hands_free.cancel_ask()


from app.ui.controller_parts.background import ROUTINE_MAX  # noqa: E402,F401 (kept importable here)


def model_choice_notice(status) -> str:
    """At start: say so when answers don't come from the "jarvis" model (the 2.5+ way to use the best model)."""
    from app.ai.jarvis_model import is_jarvis

    selected = getattr(status, "selected", None)
    if selected is None or is_jarvis(selected.name):
        return ""
    if any(is_jarvis(m.name) for m in getattr(status, "models", ())):
        return (f"Answers come from {selected.name}, but your \"jarvis\" model is installed. "
                "Say \"use the best model\" to switch (or set AI_MODEL=jarvis).")
    return (f"Answers come from {selected.name}. Say \"compare models\" to find the best one for you, "
            "then \"use the best model\".")


from app.ui.controller_parts.background import BackgroundMixin  # noqa: E402
from app.ui.controller_parts.features import FeaturesMixin  # noqa: E402
from app.ui.controller_parts.health import HealthMixin  # noqa: E402
from app.ui.controller_parts.proactive import ProactiveMixin  # noqa: E402
from app.ui.controller_parts.remote import RemoteMixin  # noqa: E402
from app.ui.controller_parts.voice import VoiceMixin  # noqa: E402


class AssistantController(FeaturesMixin, HealthMixin, RemoteMixin, BackgroundMixin, VoiceMixin, ProactiveMixin, QObject):
    """The GUI's connection to the assistant. The methods live in app/ui/controller_parts/ by concern;
    this class keeps the signals, start-up, the request flow (send -> reply), state and confirmations."""

    state_changed = Signal(str)
    _rewarm = Signal()
    _dim_request = Signal(int, object)  # software screen dimming: (level, threading.Event)
    step_aside_requested = Signal(object)  # the window fades into the mini orb; the object: a threading.Event or None
    _gpu_fit_checked = Signal()  # a warm-up finished: tell the user once if the model doesn't fit the GPU  # from a worker thread: warm the model up again (on the window's thread)
    display_settings_changed = Signal()   # 3D core quality / speed / glow / motion changed
    mic_muted_changed, transcribing = Signal(bool), Signal(bool)   # mic mute (nothing listens) / command -> text
    user_message = Signal(str)
    assistant_started = Signal()
    assistant_delta = Signal(str)
    assistant_finished = Signal(object)  # AssistantReply
    tool_event = Signal(str)
    status_changed = Signal(object)  # AIStatus
    confirmation_requested = Signal(object)  # PendingConfirmation
    microphone_active = Signal(bool)
    speaking_changed = Signal(bool)
    browser_active = Signal(bool)
    screen_capture_active = Signal(bool)
    hands_free_state = Signal(str)  # starting | waiting | command | follow_up | busy | stopped | error
    _hands_free_result = Signal(object)  # ListenResult, from the hands-free thread
    _health_ok = Signal()
    _health_failed = Signal()
    reminder = Signal(str, str)  # (title, message) from automations
    level_changed = Signal(float)
    notice = Signal(str)
    voice_engine_changed = Signal(str)  # e.g. "large-v3-turbo on GPU"
    partial_transcript = Signal(str)  # live text while you speak
    timing = Signal(object)  # dict: heard / ai / voice seconds for the last request
    confirmation_answered = Signal(object, bool)  # (PendingConfirmation, approved) answered by voice
    _restart_hands_free = Signal()
    camera_active = Signal(bool)  # privacy indicator for presence detection
    _suggestion_accepted = Signal(str)
    _remote_send = Signal(str)
    mobile_pending = Signal(object)  # a JARVIS Mobile phone asked to pair: the window asks "trust it?"
    _listen_done = Signal(object)  # ListenResult, from the recording thread
    _notes_request = Signal(str)  # "start" / "stop" meeting notes (from the tool thread)
    notes_active = Signal(bool)  # meeting / lecture notes are being taken
    notes_line = Signal(str)  # the latest sentence written into the notes
    jarvis_action = Signal(str)  # a Jarvis screen / feature asked for by voice (the window opens it)
    _setting_request = Signal(str, str)  # (field, value) changed by voice
    notifications_changed = Signal(int)  # unread notifications
    _emergency_request = Signal()  # emergency stop asked for by voice (runs on the window's thread)
    _cancel_request = Signal()     # "cancel" from a phone: stop the current request only (window's thread)
    _stop_remote_request = Signal()

    def __init__(
        self,
        ctx: "AppContext",
        *,
        ai: AIManager | None = None,
        tts: TTSManager | None = None,
        microphone: Microphone | None = None,
        stt: SpeechToText | None = None,
        runner: AsyncRunner | None = None,
    ) -> None:
        super().__init__()
        self.ctx = ctx
        s = ctx.settings
        self.runner = runner or AsyncRunner("assistant-loop")
        self.runner.start()
        self.ai = ai or AIManager.from_settings(s, SettingsStore(ctx.database))
        self.executor = create_tool_executor(ctx, GuiConfirmer(self), ai=self.ai,
                                             on_screen_capture=self.screen_capture_active.emit)
        self.executor.context.on_browser_active = self.browser_active.emit
        self.executor.context.notify = self._notify
        self.conversation = ConversationManager(
            self.ai, s, ConversationStore(ctx.database), tools=self.executor,
            on_tool=self._tool_done,
            on_tool_start=self._tool_started,
        )
        self.tts = tts or TTSManager(s, ctx.paths.models_dir / "piper")
        self.tts.on_speaking_change = self._speaking
        self.microphone = microphone or Microphone(
            s.microphone_device, enabled=s.microphone_enabled,
            on_active_change=self.microphone_active.emit, on_level=self.level_changed.emit,
        )
        if stt is None:
            from app.cli_voice import create_stt

            stt = create_stt(ctx)
        self.stt = stt
        if hasattr(stt, "on_model_change"):
            stt.on_model_change = self.voice_engine_changed.emit  # from a background thread: queued to the UI
        self.pipeline = VoicePipeline(s, self.microphone, self.stt, self.tts, self.conversation,
                                      vad_factory=lambda: create_vad(self.ctx.settings.vad_engine))
        self.speak_typed_replies = s.speak_typed_replies

        from app.core.state import StateMachine

        self.machine = StateMachine(on_change=lambda state: self.state_changed.emit(state.value))
        self._busy = False
        self._future: concurrent.futures.Future | None = None
        self._stop_listening: threading.Event | None = None
        self._listening = False
        self._last_heard: str | None = None
        self._pending_confirmation: PendingConfirmation | None = None
        self.last_status: AIStatus | None = None
        self.status_changed.connect(self._remember_status)
        self._ai_was_ok: bool | None = None
        self._last_warm_up = -1e9
        self._alert_timer: QTimer | None = None
        self._alert_state: dict | None = None
        self.presence = None
        import time as _time

        self._last_activity = _time.monotonic()
        self._model_unloaded = False
        self._suggested: dict = {}
        self._timing: dict = {}  # stage timestamps of the current request (for the HUD timing bar)
        from app.utils.speed import SpeedLog

        self.speed_log = SpeedLog(getattr(ctx.paths, "data_dir", None))
        self._status_retry = self.STATUS_RETRY_START
        self._ollama_start_tried = False
        self._closing = False
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_next = None
        self._status_timer.timeout.connect(lambda: self._status_next and self._status_next())
        self.status_changed.connect(self._after_status)
        self._health_ok.connect(lambda: self._schedule(self.HEALTH_CHECK_SECONDS, self._health_check))
        self._health_failed.connect(self.refresh_status)
        self._listen_done.connect(self._on_listen_done)
        self._hands_free_result.connect(self._on_hands_free_result)
        self._restart_hands_free.connect(self.start_hands_free)
        self._notes_request.connect(self._notes_action)
        self.partial_transcript.connect(lambda _text: self.warm_up_model())  # start loading while you speak
        self.notes = None  # app.voice.meeting.MeetingNotes while taking notes
        self._notes_resume_hands_free = False
        self.executor.context.notes = self._notes_control
        self.executor.context.jarvis_control = self._jarvis_control
        self.executor.context.apply_setting = self._apply_setting
        self.executor.context.dictation = self._dictation_control
        self.executor.context.focus = self._focus_control
        self.executor.context.step_aside = self.step_aside_blocking
        self.executor.context.dim_screen = self.dim_screen_blocking
        from app.core import context_engine

        context_engine.ENGINE.pending_approval = lambda: (
            getattr(getattr(self._pending_confirmation, "request", None), "summary", None)
            if getattr(self, "_pending_confirmation", None) is not None else None)
        self._dimmer = None
        self._dim_request.connect(self._dim_now)
        self.executor.context.mode_control = self._mode_control
        self.executor.context.emergency = self._emergency_control
        self._emergency_request.connect(self.emergency_stop)
        self._cancel_request.connect(self.cancel)
        self._stop_remote_request.connect(self.stop_remote)
        self._focus = None  # app.agent.focus.FocusSession while focusing
        self._focus_timer = None
        self.init_proactive()  # event bus -> notification center, modes, emergency switches
        self._setting_request.connect(self._apply_setting_now)
        self.dictating = False
        self.conversation.on_memory_suggestion = lambda facts: self.notice.emit("Worth remembering? " + "; ".join(
            f"\"{f}\"" for f in facts) + "  (Menu -> Memory -> Suggestions: accept or reject. Nothing is saved until you accept.)")
        self.conversation.on_correction = self._on_correction     # SPEECH_DATASET (app/voice/speech_dataset.py)
        self._downloads_timer = QTimer(self)
        self._downloads_timer.setInterval(5 * 60 * 1000)
        self._downloads_timer.timeout.connect(self._sort_downloads)
        if ctx.settings.auto_sort_downloads:
            self._downloads_timer.start()
        self._start_clipboard_memory()
        self._suggestion_accepted.connect(lambda text: self.send_text(text, spoken=True))
        self.voice_engine_changed.connect(self._gpu_memory_saver)
        self.voice_levels: deque[float] = deque(maxlen=240)   # the microphone while you spoke (0..1), for the chat waveform
        self.last_message_spoken = False
        self._mic_on = False
        self.microphone_active.connect(self._mic_changed)
        self.level_changed.connect(self._collect_level)
        self._rewarm.connect(self.warm_up_model)
        self._gpu_fit_checked.connect(self._report_gpu_fit)
        self._remote_send.connect(lambda text: self.send_text(text, source="phone"))
        self.remote = None
        self.remote_fingerprint = ""
        self.remote_hub = None
        self.hands_free = None  # HandsFreeListener while always-listening is on
        self.tts.start()
        self.tts.prewarm(self._usual_phrases(), delay=15.0)  # the wake greeting etc., ready soon after start
        self.scheduler = None
        if ctx.database is not None:
            from app.automation.scheduler import Scheduler
            from app.automation.tasks import AutomationStore
            from app.tools.automation import seed_default_workflows

            try:
                seed_default_workflows(self.executor.context)
            except Exception:
                _log.warning("could not add the default workflows", exc_info=True)
            self.scheduler = Scheduler(AutomationStore(ctx.database), self.executor, self._notify)
            self.executor.context.scheduler = self.scheduler
            from app.core.upkeep import Upkeep

            self.upkeep = Upkeep(ctx.database, ctx.paths.db_path, ctx.paths.data_dir / "backups", settings=ctx.settings)
            self.scheduler.housekeeping.append(self.upkeep.run)  # health history + the daily backup, off the UI thread

    _update_found = Signal(object)


    # ----------------------------------------------------------- meeting notes


    HINTS_EVERY = 30 * 60.0
    _hints_at = 0.0


    # ------------------------------------------------------- voice-only control


    _DICTATION_STOP = ("stop dictation", "end dictation", "stop typing", "dictation off", "ডিক্টেশন বন্ধ", "লেখা বন্ধ")
    _DICTATION_WORDS = ((r"\s*\b(?:new line|next line)\b\s*", "\n"), (r"\s*\bnew paragraph\b\s*", "\n\n"),
                        (r"\s*\b(?:comma)\b", ","), (r"\s*\b(?:full stop|period)\b", "."),
                        (r"\s*\bquestion mark\b", "?"), (r"\s*\bexclamation mark\b", "!"),
                        (r"\s*\b(?:colon)\b", ":"))


    WEEKLY_DAYS = 7.0
    WEEKLY_IDLE_MINUTES = 15.0
    _health_running = False


    _best_model: str | None = None


    # ---------------------------------------------------------------- proactive alerts

    ALERT_SECONDS = 30.0


    # ------------------------------------------------------------ watchdog, memory, suggestions


    SAVER_CONTEXT = 6144  # tokens: fits a 4B model + speech recognition in 6 GB of GPU memory


    # ------------------------------------------------------------ focus mode


    # ------------------------------------------------------------------ state

    @property
    def state(self) -> str:
        return self._state

    @property
    def busy(self) -> bool:
        return self._busy or self._listening

    @property
    def _state(self) -> str:
        return self.machine.state.value

    @_state.setter
    def _state(self, value: str) -> None:
        self.machine.to(value, "direct")

    def _set_state(self, state: str, reason: str = "") -> None:
        self.machine.to(state, reason)

    def check_state(self) -> None:
        """Watchdog (every few seconds from the window's timer): a busy state that isn't true any more ends."""
        self.machine.check(self._idle_state)

    def _idle_state(self) -> str:
        if self._listening:
            return "LISTENING"
        if self._busy:
            return "THINKING"
        return "SPEAKING" if self.tts.is_speaking else "IDLE"

    # Opening or searching something puts that app on screen, so Jarvis gets out of the way (STEP_ASIDE).
    SHOWS_SOMETHING = frozenset({
        "open_application", "open_website", "search_in_browser", "play_music", "open_path", "open_in_editor",
        "run_workflow", "open_settings_page", "whatsapp_message", "browser_open", "window_control",
        "youtube_control", "arrange_windows", "run_project",
    })

    STAYS = {"youtube_control": {"now_playing", "show_queue", "clear_queue", "queue", "repeat"},
             "arrange_windows": {"list", "save", "delete"}}

    def _tool_done(self, execution) -> None:
        self.tool_event.emit(describe_execution(execution))
        if execution.result.ok and execution.call.name in self.SHOWS_SOMETHING and self._step_aside_on():
            action = execution.call.arguments.get("action")
            if execution.call.name == "window_control" and action in ("minimize", "minimize_all"):
                return
            if execution.call.name in self.STAYS and action in self.STAYS[execution.call.name]:
                return  # an answer, not something new on screen
            self.step_aside_requested.emit(None)

    def _step_aside_on(self) -> bool:
        s = self.ctx.settings
        return getattr(s, "step_aside", True) and not getattr(s, "always_on_top", False)

    def step_aside_blocking(self, timeout: float = 1.2) -> None:
        """From a tool's thread: fade Jarvis's window away and wait until it's gone (keys go to your app)."""
        import threading as _threading

        if not self._step_aside_on():
            return
        done = _threading.Event()
        self.step_aside_requested.emit(done)
        done.wait(timeout)

    def dim_screen_blocking(self, level: int, timeout: float = 2.0) -> bool:
        """From a tool's thread: the software dimmer (the window's thread shows it). True = done."""
        import threading as _threading

        done = _threading.Event()
        self._dim_request.emit(int(level), done)
        return done.wait(timeout)

    def _dim_now(self, level: int, done) -> None:
        try:
            if self._dimmer is None:
                from app.ui.dimmer import ScreenDimmer

                self._dimmer = ScreenDimmer()
            self._dimmer.set_level(level)
        finally:
            if done is not None:
                done.set()

    def _tool_started(self, call: ToolCall) -> None:
        self._set_state_from_any_thread("EXECUTING")

    def _set_state_from_any_thread(self, state: str, reason: str = "") -> None:
        self.machine.to(state, reason)

    def _speaking(self, speaking: bool) -> None:
        if not speaking and getattr(self, "_last_reply", None):
            import time as _time

            self._last_reply = (self._last_reply[0], _time.monotonic())  # the echo window starts when it stops
        if speaking and self._timing.get("sent") and "audio" not in self._timing:
            import time as _time

            self._timing["audio"] = _time.monotonic()
            self._emit_timing()
        self.speaking_changed.emit(speaking)
        if not self._busy and not self._listening:
            self._set_state_from_any_thread("SPEAKING" if speaking else "IDLE")
        # If a reply is still finishing, _finished() will pick the right state:
        # the TTS manager is already idle when it reports speaking=False.

    # --------------------------------------------------------------- startup

    STATUS_RETRY_START = 3.0  # seconds; doubles up to STATUS_RETRY_MAX while the AI can't be reached
    STATUS_RETRY_MAX = 30.0
    HEALTH_CHECK_SECONDS = 60.0

    def refresh_status(self) -> None:
        future = self.runner.submit(self.ai.status())
        future.add_done_callback(lambda f: self.status_changed.emit(f.result()) if not f.exception() else None)

    def _status_ok(self, status: AIStatus) -> bool:
        return bool(status.reachable and status.selected is not None)

    def _after_status(self, status: AIStatus) -> None:
        """At sign-in Jarvis often starts before Ollama: keep checking until it answers, then say so."""
        self._last_status = status
        ok = self._status_ok(status)
        was_ok = self._ai_was_ok
        self._ai_was_ok = ok
        if ok:
            self._status_retry = self.STATUS_RETRY_START
            if was_ok is not True:
                self.warm_up_model()
                notice = model_choice_notice(status)
                if notice:
                    self.notice.emit(notice)
            if was_ok is False:
                self.notice.emit(f"Connected to the AI model ({status.selected.name}).")
            if self._state == "ERROR":  # the AI answers again: the problem is over
                self._set_state(self._idle_state())
            self._schedule(self.HEALTH_CHECK_SECONDS, self._health_check)
            return
        if not status.reachable and not self._busy and self._state in ("IDLE", "ERROR"):
            self._set_state("ERROR")  # shown as PROBLEM until the AI answers (then cleared above)
        if not status.reachable and not self._ollama_start_tried:
            self._ollama_start_tried = True
            self._try_start_ollama()
        delay = self._status_retry
        self._status_retry = min(self.STATUS_RETRY_MAX, self._status_retry * 2)
        self._schedule(delay, self.refresh_status)

    def _try_start_ollama(self) -> None:
        from app.ai.ollama import is_loopback_host

        s = self.ctx.settings
        if not (s.auto_start_ollama and s.ai_provider == "ollama" and is_loopback_host(s.ollama_host)):
            return
        from app.utils.ollama_service import start_ollama

        try:
            if start_ollama(getattr(s, "ollama_fast_memory", True)):
                self.notice.emit("Starting Ollama... I'll connect as soon as it's ready.")
        except Exception:
            _log.warning("starting Ollama failed", exc_info=True)

    def _schedule(self, seconds: float, callback) -> None:
        if self._closing:
            return
        self._status_next = callback
        self._status_timer.start(int(seconds * 1000))

    def _health_check(self) -> None:
        """Cheap check while all is well; a full status refresh only if the AI stopped answering."""
        future = self.runner.submit(self.ai.provider.health())

        def done(f) -> None:
            if f.cancelled() or f.exception() is not None:
                self._health_failed.emit()
            else:
                self._health_ok.emit()

        future.add_done_callback(done)

    # ------------------------------------------------------------------ text

    @Slot(str)
    def send_text(self, text: str, *, spoken: bool = False, source: str | None = None) -> bool:
        text = text.strip()
        if not text:
            return False
        if self._live_control(text):          # stop speaking / pause the mission / explain... even while busy
            return True
        if self._busy:
            self.notice.emit("Still working on the previous request. Press Esc to cancel it.")
            return False
        if not spoken:
            self._last_heard = None
            self.tts.stop()
        self._busy = True
        import time as _time
        heard = self._timing.get("heard") if spoken else None
        voice_event = self._timing.get("voice_event") if spoken else None
        self._last_activity = _time.monotonic()
        self._model_unloaded = False
        self._timing = {"sent": _time.monotonic(), "heard": heard, "spoken": spoken or self.speak_typed_replies,
                        "voice_event": voice_event}
        self.last_message_spoken = bool(spoken)
        self.user_message.emit(text)
        self.assistant_started.emit()
        self._set_state("THINKING")
        self.conversation.spoken = spoken
        speak = spoken or self.speak_typed_replies
        self._future = self.runner.submit(self._respond(text, speak, source or ("voice" if spoken else "text")))
        self._future.add_done_callback(self._finished)
        return True

    async def _respond(self, text: str, speak: bool, source: str = "text") -> AssistantReply:
        splitter = SentenceSplitter(quick_start=True)       # starts speaking at the first clause, not the first full stop
        self._reply_so_far = ""

        def on_delta(delta: str) -> None:
            self._reply_so_far = (self._reply_so_far + delta)[-4000:]
            if "first" not in self._timing:
                import time as _time

                self._timing["first"] = _time.monotonic()
            if self._state == "EXECUTING":
                self._set_state_from_any_thread("THINKING")
            self.assistant_delta.emit(delta)
            if speak:
                for sentence in splitter.feed(delta):
                    self.tts.speak(sentence)

        reply = await self.conversation.send(text, on_delta=on_delta, source=source)
        import time as _time

        self._last_reply = ((reply.text or self._reply_so_far)[-4000:], _time.monotonic() + 60.0)  # while spoken
        self._reply_so_far = ""
        if speak:
            rest = splitter.flush()
            if reply.ok and rest:
                self.tts.speak(rest)
            elif not reply.ok:
                self.tts.speak(reply.text)
        self.notice.emit(reply.checklist) if getattr(reply, "checklist", "") else self._emit_sources(reply)
        return reply

    def _emit_timing(self) -> None:
        t = self._timing
        if not t.get("sent") or t.get("reported"):
            return
        t["reported"] = True
        first = t.get("first") or t.get("done")
        result = {"heard": t.get("heard"), "ai": (first - t["sent"]) if first else None,
                  "voice": (t["audio"] - first) if t.get("audio") and first else None}
        self.timing.emit(result)
        try:
            if t.get("voice_event") and first and self.ctx.database is not None:
                from app.core.voice_stats import VoiceStats

                VoiceStats(self.ctx.database).first_reply(t["voice_event"], int((first - t["sent"]) * 1000))
        except Exception:
            _log.warning("could not record the first-reply time", exc_info=True)
        try:
            t["entry"] = self.speed_log.add(result)
            if t.get("model"):
                t["entry"]["model"] = t["model"]
                self.speed_log.save()
        except Exception:
            _log.warning("could not record the timing", exc_info=True)

    def _finished(self, future: concurrent.futures.Future) -> None:
        import time as _time

        self._timing["done"] = _time.monotonic()
        if not self._timing.get("spoken"):
            self._emit_timing()
        self._busy = False
        self._future = None
        if self.hands_free is not None:  # listen again once the reply has been spoken
            self.hands_free.resume(follow_up=self.conversation.spoken and not future.cancelled())
        if future.cancelled():
            self.assistant_finished.emit(AssistantReply("Cancelled.", ok=False, error="cancelled"))
            self._set_state_from_any_thread(self._idle_state())
            return
        exc = future.exception()
        if exc is not None:
            _log.error("reply failed", exc_info=exc)
            self.assistant_finished.emit(AssistantReply("Something went wrong. The details are in the log.", ok=False, error="internal"))
            self._set_state_from_any_thread("ERROR")
            return
        reply = future.result()
        self._timing["model"] = reply.model or ""
        if self._timing.get("entry") is not None:
            self._timing["entry"]["model"] = reply.model or ""
            self.speed_log.save()
        self.assistant_finished.emit(reply)
        if not reply.ok and reply.error in ("provider_unavailable", "no_model", "model_not_found"):
            self._set_state_from_any_thread("ERROR")
        else:
            self._set_state_from_any_thread(self._idle_state())

    # ------------------------------------------------------------------ voice


    # -------------------------------------------------------------- hands-free


    # ------------------------------------------------------------ voice confirmation / verification


    ECHO_SECONDS = 10.0  # its voice can still come back from the speakers this long after it stops


    WARM_UP_EVERY = 120.0  # seconds; keeps the model loaded while you use Jarvis

    _gpu_fit_reported = False


    # ---------------------------------------------------------------- control

    @Slot()
    def cancel(self) -> None:
        """Esc: stop speaking / listening, cancel the request, deny a pending question, stop a shutdown countdown."""
        self.tts.stop()
        from app.tools.windows import cancel_pending_power

        if cancel_pending_power():
            self.notice.emit("Restart / shutdown cancelled.")
            self.tts.speak("Cancelled.")
        self.stop_listening()
        pending = self._pending_confirmation
        if pending is not None and not pending.future.done():
            pending.future.set_result(False)
        if self._future is not None:
            self._future.cancel()
        log_event(_log, "cancelled_by_user")

    def resolve_confirmation(self, pending: PendingConfirmation, approved: bool) -> None:
        if not pending.future.done():
            pending.future.set_result(bool(approved))

    def _remember_status(self, status: AIStatus) -> None:
        self.last_status = status

    def installed_models(self) -> list[str]:
        status = self.last_status
        return [m.name for m in status.models if m.can_chat] if status else []

    def apply_settings(self, settings, changed: set[str]) -> None:
        """Apply the settings that can change while running; the rest need a restart."""
        if changed & live_fields.DISPLAY_FIELDS:
            self.ctx.settings = settings
            self.display_settings_changed.emit()
        if "tts_speed" in changed:
            self.tts.speed = settings.tts_speed
        if "tts_volume" in changed:
            self.tts.volume = settings.tts_volume
        if "tts_enabled" in changed:
            self.tts.enabled = settings.tts_enabled
        if "speak_typed_replies" in changed:
            self.speak_typed_replies = settings.speak_typed_replies
        if "direct_commands" in changed:
            self.conversation.direct_commands = settings.direct_commands
        if "start_with_windows" in changed:
            from app.utils import autostart

            try:
                autostart.set_enabled(settings.start_with_windows)
                self.notice.emit("The assistant will start with Windows." if settings.start_with_windows
                                 else "The assistant will no longer start with Windows.")
            except OSError as exc:
                self.notice.emit(f"Could not change the Windows startup setting: {exc}")
        if "auto_sort_downloads" in changed:
            self.ctx.settings = settings
            if settings.auto_sort_downloads:
                self._downloads_timer.start()
                self._sort_downloads()
            else:
                self._downloads_timer.stop()
        if changed & {"presence_detection", "presence_lock", "presence_away_minutes"}:
            self.ctx.settings = settings
            self.stop_presence()
            self.start_presence()
        if changed & HANDS_FREE_FIELDS:
            self.ctx.settings = settings
            was_on = self.hands_free is not None
            self.stop_hands_free()
            if settings.hands_free:
                self.start_hands_free()
            elif was_on:
                self.notice.emit("Hands-free is off. Press Talk (or the hotkey) to speak.")
        if changed & SCREEN_FIELDS:
            self.executor.context.settings = settings  # screen tools read their settings on every call
        if changed & {"home_location", "temperature_unit", "proactive_alerts", "weekly_health_check", "low_battery_percent", "barge_in",
                      "voice_interrupt", "voice_confirm", "ai_fast_model", "conversation_memory", "document_folders",
                      "embedding_model", "update_manifest_url", "proactive_suggestions", "idle_unload_minutes",
                      "ai_smart_model", "home_assistant_url", "home_assistant_token", "custom_vocabulary",
                      "stt_second_pass", "memory_suggestions"}:
            self.conversation.settings = settings
            self.ctx.settings = settings  # read on every use
            self.executor.context.settings = settings
            if changed & {"ai_fast_model", "ai_smart_model"} and hasattr(self.ai, "settings"):
                self.ai.settings = settings
            if "stt_second_pass" in changed and hasattr(self.stt, "second_pass"):
                self.stt.second_pass = settings.stt_second_pass
            if "custom_vocabulary" in changed:
                self.refresh_hints()
        if "ai_model" in changed and settings.ai_model:
            future = self.runner.submit(self.ai.set_model(settings.ai_model))

            def done(f: concurrent.futures.Future) -> None:
                if f.exception() is not None:
                    self.notice.emit(str(f.exception()))
                else:
                    self.notice.emit(f"Now using {f.result().name}.")
                    self.refresh_status()

            future.add_done_callback(done)
        if changed & REMOTE_FIELDS:
            self.apply_remote_settings(settings)
        self.ctx.settings = self.executor.context.settings = settings  # what was saved is what Jarvis now uses
        log_event(_log, "settings_applied", changed=sorted(changed))

    def set_muted(self, muted: bool) -> None:
        self.tts.set_muted(muted)

    def reset_conversation(self) -> None:
        self.conversation.reset()

    def shutdown(self) -> None:
        self._closing = True
        self._status_timer.stop()
        if self.notes is not None:  # the transcript is already on disk; just stop listening
            try:
                self.notes.stop(timeout=5)
            except Exception:
                pass
            self.notes = None
        self.stop_hands_free()
        self.stop_presence()
        self.stop_remote()
        hub = getattr(self.executor, "mcp_hub", None)
        if hub is not None:
            hub.close()
        self.cancel()
        closers = [self.executor.context.aclose(), self.ai.aclose()]
        if self.scheduler is not None:
            closers.insert(0, self.scheduler.stop())
        for closing in closers:
            try:
                self.runner.run(closing, timeout=10)
            except Exception:
                _log.warning("shutdown step failed", exc_info=True)
        self.runner.stop()
        self.tts.close()
