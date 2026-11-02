"""Background helpers: presence, scheduler, alerts, housekeeping, watchdog, GPU memory, suggestions, focus mode, PC watcher.

Part of AssistantController (app/ui/controller.py), split out in 3.4.1; the methods use its attributes
and signals through ``self``."""

from __future__ import annotations

from PySide6.QtCore import QTimer

from app.ai.base import ToolCall
from app.memory.settings_store import SettingsStore
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("ui.controller")

ROUTINE_MAX = 4  # apps offered together as "your usual set"


class BackgroundMixin:
    def ensure_bangla_voice(self) -> None:
        """Download the offline Bangla voice in the background (once), when you speak Bangla."""
        s = self.ctx.settings
        if "bn" not in s.allowed_languages:
            return
        from app.tts import bangla
        from app.utils import network

        models = self.ctx.paths.models_dir
        if bangla.is_installed(models) or network.is_offline_now(s):
            return
        import threading as _threading

        def work() -> None:
            try:
                bangla.download(models)
                self.notice.emit("The offline Bangla voice is installed: Bangla replies are spoken even without "
                                 "internet (from the next start).")
            except Exception as exc:
                log_event(_log, "bangla_voice_download_failed", detail=str(exc)[:160])

        _threading.Thread(target=work, name="bangla-voice", daemon=True).start()

    def attach_camera_indicator(self) -> None:
        """CAMERA ACTIVE follows the real webcam (presence detection and gesture control share it)."""
        from app.gesture.camera import shared_broker

        shared_broker(on_active=self.camera_active.emit)

    def start_presence(self) -> None:
        """Opt-in (PRESENCE_DETECTION): lock the PC when you leave, greet you when you're back."""
        s = self.ctx.settings
        self.attach_camera_indicator()          # always: gesture control may use the camera even when presence detection is off
        if not s.presence_detection or self.presence is not None:
            return
        from app.vision.presence import PresenceMonitor

        def away() -> None:
            if self.ctx.settings.presence_lock:
                self.notice.emit("You've stepped away: locking the PC.")
                self.runner.submit(self.executor.execute(ToolCall("power_control", {"action": "lock"}),
                                                         user_command=True))

        def back(seconds: float) -> None:
            if seconds >= 120:
                address = f", {s.user_address}" if s.user_address else ""
                message = f"Welcome back{address}."
                self.notice.emit(message)
                self.tts.speak(message)

        self.presence = PresenceMonitor(away_minutes=s.presence_away_minutes, on_away=away, on_return=back)
        self.presence.start()

    def stop_presence(self) -> None:
        if self.presence is not None:
            self.presence.stop()
            self.presence = None

    def start_scheduler(self) -> None:
        if self.scheduler is not None:
            async def start() -> None:
                self.scheduler.start()

            self.runner.submit(start())
        self.start_alerts()
        try:
            self.start_remote()
        except Exception:
            _log.warning("phone remote failed", exc_info=True)
        try:
            self.ensure_bangla_voice()
        except Exception:
            _log.warning("could not start the Bangla voice download", exc_info=True)
        try:
            self.start_presence()
        except Exception:
            _log.warning("presence detection could not start", exc_info=True)

    def start_alerts(self) -> None:
        """Speak up without being asked: low battery, charger plugged in or out (PROACTIVE_ALERTS)."""
        if self._alert_timer is not None:
            return
        self._alert_timer = QTimer(self)
        self._alert_timer.setInterval(int(self.ALERT_SECONDS * 1000))
        self._alert_timer.timeout.connect(self.check_alerts)
        self._alert_timer.timeout.connect(self.housekeeping)
        self._alert_timer.timeout.connect(self.watch_pc)
        self._alert_timer.start()

    def housekeeping(self) -> None:
        """Every 30 s: restart parts that stopped, free idle memory, maybe suggest something."""
        for step in (self.watchdog, self.unload_if_idle, self.maybe_suggest, self.weekly_health_check,
                     self._refresh_hints_now_and_then, self.proactive_tick):
            try:
                step()
            except Exception:
                _log.warning("housekeeping step %s failed", step.__name__, exc_info=True)

    def watchdog(self) -> list[str]:
        """Restart background parts that died, instead of Jarvis silently going deaf. Returns what restarted."""
        restarted: list[str] = []
        s = self.ctx.settings
        listener = self.hands_free
        if s.hands_free and listener is not None and not listener.running and not self._closing:
            self.hands_free = None
            self.start_hands_free()
            restarted.append("listening")
        worker = getattr(self.tts, "_worker", None)
        if worker is not None and not worker.is_alive() and not self._closing:
            self.tts._worker = None
            self.tts.start()
            restarted.append("speech")
        if self.presence is not None and not self.presence.running and s.presence_detection:
            problem = self.presence.problem
            self.presence = None
            if problem:
                self.notice.emit(problem)
            else:
                self.start_presence()
                restarted.append("presence")
        scheduler = self.scheduler
        task = getattr(scheduler, "_task", None) if scheduler is not None else None
        if task is not None and task.done() and not self._closing:  # the automation loop crashed: start it again
            scheduler._task = None
            try:
                self.runner.submit(self._start_scheduler_again(scheduler))
                restarted.append("automations")
            except Exception:
                _log.warning("could not restart the automations", exc_info=True)
        import time as _time

        if self._ai_ok_flag() is False and not self._closing:  # the AI has been unreachable: try Ollama again
            last = getattr(self, "_ollama_retry_at", 0.0)
            if _time.monotonic() - last > 300:
                self._ollama_retry_at = _time.monotonic()
                self._try_start_ollama()
        if restarted:
            log_event(_log, "watchdog_restarted", parts=restarted)
            self.notice.emit(f"Restarted {', '.join(restarted)} (it had stopped).")
        return restarted

    async def _start_scheduler_again(self, scheduler) -> None:
        scheduler.start()

    def _ai_ok_flag(self) -> bool | None:
        status = getattr(self, "_last_status", None)
        if status is None:
            return None
        return bool(getattr(status, "reachable", False))

    def _gpu_memory_saver(self, description: str) -> None:
        """Speech recognition moved onto a small GPU: shrink the AI's context so both stay on the GPU
        (if they don't fit, Ollama moves layers to the CPU and every answer becomes slow)."""
        if not self.ctx.settings.gpu_memory_saver or "GPU" not in description:
            return
        import threading as _threading

        def work() -> None:
            from app.tools.system import gpu_info

            gpus = gpu_info().get("gpus") or []
            try:
                total = float(gpus[0]["memory_total_mb"]) if gpus else 0.0
            except (KeyError, ValueError):
                total = 0.0
            provider = self.ai.provider
            if 0 < total <= 8192 and getattr(provider, "context_window", 0) > self.SAVER_CONTEXT:
                provider.context_window = self.SAVER_CONTEXT
                log_event(_log, "gpu_memory_saver", gpu_mb=int(total), context=self.SAVER_CONTEXT)
                self._last_warm_up = -1e9  # load it again now at the new size, not on the first question
                self._rewarm.emit()

        _threading.Thread(target=work, name="gpu-saver", daemon=True).start()

    def unload_if_idle(self) -> bool:
        """IDLE_UNLOAD_MINUTES without a request: free the AI model's memory (it reloads on "Jarvis")."""
        import time as _time

        minutes = self.ctx.settings.idle_unload_minutes
        if not minutes or self._model_unloaded or self._busy or self._health_running:
            return False
        if _time.monotonic() - self._last_activity < minutes * 60:
            return False
        status = self.last_status
        if status is None or status.selected is None:
            return False
        self._model_unloaded = True
        self._last_warm_up = -1e9  # the next wake loads it again
        self.runner.submit(self.ai.provider.unload(status.selected.name))
        log_event(_log, "model_unloaded_idle", minutes=minutes)
        return True

    PROACTIVE_SECONDS = 600

    def proactive_tick(self, now=None) -> list:
        """Every 10 minutes: the proactive engine (app/core/proactive.py) - told, never executed."""
        import time as _time

        from app.core import events, modes, proactive

        if self._busy or self.tts.is_speaking or self._pending_confirmation or self.ctx.database is None:
            return []
        last = getattr(self, "_proactive_at", 0.0)
        if now is None and _time.monotonic() - last < self.PROACTIVE_SECONDS:
            return []
        self._proactive_at = _time.monotonic()
        from datetime import datetime as _dt

        delivered = []
        for decision in proactive.scan(self.executor.context, now or _dt.now().astimezone()):
            if decision.outcome == "drop":
                continue
            c = decision.candidate
            if decision.outcome == "speak" and modes.STATE.current.suggestions:
                self.speak_up(c.message, "urgent" if c.emergency else "normal", events.SUGGESTION, action=c.action)
            else:
                events.BUS.publish(events.SUGGESTION, message=c.message, action=c.action)
            delivered.append(decision)
        return delivered

    def maybe_suggest(self, now=None) -> str | None:
        """Suggest an app you usually open around now (once a day each), only while idle and listening."""
        s = self.ctx.settings
        from app.core import modes

        if not s.proactive_suggestions or not modes.STATE.current.suggestions or self._busy or self.tts.is_speaking \
                or self._pending_confirmation:
            return None
        if self._focus is not None:  # focus mode: no "shall I open ...?"
            return None
        if self.presence is not None and not self.presence.present:
            return None
        from datetime import datetime as _dt

        from app.agent.habits import suggestions

        now = now or _dt.now().astimezone()
        day = now.strftime("%Y-%m-%d")
        if self._suggested.get("day") != day:
            self._suggested = {"day": day, "apps": set()}
        found = suggestions(self.ctx.database, now, skip=self._suggested["apps"])
        if not found:
            return None
        from app.agent.habits import routine_question

        group = found[:ROUTINE_MAX]  # your usual set for this time of day, offered together
        self._suggested["apps"].update(group)
        app = group[0]
        question = routine_question(group, s.user_address)
        self.notice.emit(question)
        listener = self.hands_free
        if listener is not None and s.voice_confirm:
            from app.agent.language import classify_answer

            self.tts.speak(question + " Yes or no?")

            def on_answer(text: str) -> bool:
                answer = classify_answer(text)
                if answer is None:
                    return False
                if answer:
                    self._suggestion_accepted.emit("open " + " and ".join(group))
                return True

            listener.ask(on_answer, timeout=15)
        log_event(_log, "suggested_app", apps=len(group))
        return app

    def _alert(self, message: str, urgent: bool = False, event_name: str | None = None, **data) -> None:
        """Speak up without being asked: the notification center gets it; focus holds it, the mode decides whether
        it is spoken (urgent ones speak in every mode but presentation)."""
        self.speak_up(message, "urgent" if urgent else data.pop("priority", "high"), event_name, **data)

    def _start_clipboard_memory(self) -> None:
        """CLIPBOARD_HISTORY: Qt tells us when the clipboard changes (no polling); app/core/clipboard_memory.py decides
        what may be kept. Switching the setting on or off takes effect at once (the watcher reads it on every change)."""
        if self.ctx.database is None:
            return
        try:
            from PySide6.QtGui import QGuiApplication

            from app.core import modes
            from app.core.clipboard_memory import ClipboardStore, ClipboardWatcher

            board = QGuiApplication.clipboard()
            if board is None:
                return
            store = ClipboardStore(self.ctx.database, int(getattr(self.ctx.settings, "clipboard_retention_days", 7)))
            self._clipboard_watcher = ClipboardWatcher(store, enabled=lambda: bool(getattr(self.ctx.settings, "clipboard_history", False)),
                                                       remember=lambda: modes.STATE.current.remember)
            board.dataChanged.connect(lambda: self._clipboard_changed(board))
        except Exception:
            _log.warning("clipboard memory could not start", exc_info=True)

    def _clipboard_changed(self, board) -> None:
        watcher = getattr(self, "_clipboard_watcher", None)
        if watcher is None or not watcher.enabled():
            return
        try:
            mime = board.mimeData()
            text = board.text() if mime is not None and mime.hasText() else ""
            watcher.changed(text)
        except Exception:
            _log.debug("clipboard change ignored", exc_info=True)

    def _focus_control(self, action: str, minutes: int, close_distractions: bool, keep_closed: bool = False) -> str:
        """focus_mode tool (assistant thread)."""
        import threading as _threading

        from app.agent.focus import FocusSession, app_list, clamp_minutes

        if action == "status":
            if self._focus is None:
                return "Focus mode is off."
            return f"{self._focus.left_minutes()} minutes of focus left."
        if action == "stop":
            if self._focus is None:
                return "Focus mode wasn't on."
            return self._end_focus(stopped=True)
        minutes = clamp_minutes(minutes)
        if self._focus_timer is not None:
            self._focus_timer.cancel()
        self._focus = FocusSession(minutes)
        self._focus_timer = _threading.Timer(minutes * 60, self._end_focus)
        self._focus_timer.daemon = True
        self._focus_timer.start()
        log_event(_log, "focus_started", minutes=minutes, close=close_distractions)
        closed = self._close_distractions(app_list(self.ctx.settings.focus_apps)) if close_distractions else []
        self._focus.closed = closed
        if keep_closed:
            self._focus.keep_closed = True
            self._focus.blocked = app_list(self.ctx.settings.focus_apps)
            self._schedule_focus_guard()
        extra = f" I closed {', '.join(closed)}." if closed else ""
        extra += " I'll keep them closed." if keep_closed else ""
        return f"Focus mode for {minutes} minutes. I'll keep quiet until then.{extra}"

    def _schedule_focus_guard(self) -> None:
        import threading as _threading

        timer = _threading.Timer(30.0, self._focus_guard_tick)
        timer.daemon = True
        self._focus_guard_timer = timer
        timer.start()

    def _focus_guard_tick(self) -> list[str]:
        """Every 30 s during a keep-closed focus: close blocked apps that came back (see app/agent/focus.py)."""
        session = self._focus
        if session is None or not session.keep_closed or session.over():
            return []
        closed: list[str] = []
        try:
            import psutil

            names = [p.info["name"] or "" for p in psutil.process_iter(["name"])]
            for app in session.guard_candidates(names):
                session.note_reclosed(app)               # counted even if closing fails: no endless retries
                closed += self._close_distractions([app])
            if closed:
                log_event(_log, "focus_guard_closed", apps=len(closed))
        except Exception:
            _log.warning("focus guard check failed", exc_info=True)
        if self._focus is session:
            self._schedule_focus_guard()
        return closed

    def _close_distractions(self, names: list[str]) -> list[str]:
        """Close the apps in FOCUS_APPS that are open (you agreed when starting focus with that option)."""
        from app.tools.applications import AppNameArgs, close_application
        from app.tools.base import ToolError

        closed = []
        for name in names:
            try:
                result = close_application(AppNameArgs(name=name), self.executor.context)
            except (ToolError, ValueError):
                continue  # not open
            except Exception:
                _log.warning("could not close %s for focus", name, exc_info=True)
                continue
            if result.ok:
                closed.append(name)
        return closed

    def _end_focus(self, stopped: bool = False) -> str:
        session, self._focus = self._focus, None
        guard = getattr(self, "_focus_guard_timer", None)
        if guard is not None:
            guard.cancel()
        if self._focus_timer is not None and stopped:
            self._focus_timer.cancel()
        self._focus_timer = None
        if session is None:
            return "Focus mode is off."
        summary = session.summary()
        log_event(_log, "focus_ended", held=len(session.held), stopped=stopped)
        if not stopped:  # the timer: say it; a "stop focus" gets it as the reply
            self.notice.emit(summary)
            self.tts.speak(summary)
        return summary

    def check_alerts(self, battery=None) -> str | None:
        """One check; returns the alert spoken (for tests). ``battery`` = psutil.sensors_battery() result."""
        s = self.ctx.settings
        if not s.proactive_alerts:
            return None
        if battery is None:
            import psutil

            try:
                battery = psutil.sensors_battery()
            except Exception:
                return None
            if battery is None:
                return None
        percent, plugged = round(battery.percent), bool(battery.power_plugged)
        address = f", {s.user_address}" if s.user_address else ""
        message = None
        previous = self._alert_state
        if previous is not None and plugged != previous["plugged"]:
            message = (f"Charger connected{address}. Battery at {percent}%." if plugged
                       else f"Running on battery now{address}. {percent}% left.")
        elif not plugged:
            low, critical = s.low_battery_percent, min(10, s.low_battery_percent)
            level = "critical" if percent <= critical else "low" if percent <= low else None
            if level is not None and level != (previous or {}).get("warned"):
                message = (f"Battery critically low{address}: {percent}%. Please plug in the charger now."
                           if level == "critical" else f"Battery is at {percent}%{address}. You may want to plug in the charger.")
        warned = (previous or {}).get("warned")
        if plugged:
            warned = None
        elif message and "Battery" in message:
            warned = "critical" if "critically" in message else "low"
        self._alert_state = {"plugged": plugged, "warned": warned}
        if message:
            log_event(_log, "proactive_alert", kind="battery")
            from app.core import events

            battery_event = events.BATTERY_LOW if "Battery" in message else None
            self._alert(message, urgent="critically" in message, event_name=battery_event,
                        critical="critically" in message)
            return message
        return None

    def watch_pc(self) -> list[str]:
        """PC_WATCHER: disk almost full, an app hogging the CPU, a hot GPU, a finished download (in a thread)."""
        if not getattr(self.ctx.settings, "pc_watcher", True):
            return []
        if getattr(self, "_watcher", None) is None:
            from app.agent.watcher import Watcher
            from app.tools.housekeeping import downloads_dir

            try:
                folder = downloads_dir()
            except Exception:
                folder = None
            self._watcher = Watcher(downloads=folder)
        import threading as _threading

        def work() -> None:
            from app.core import events

            for message in self._watcher.check():
                log_event(_log, "pc_watcher_alert")
                fix = "clean temp files" if "clean temp files" in message.lower() else ""
                name = events.DOWNLOAD_COMPLETED if "download" in message.lower() else events.PC_PROBLEM
                self._alert(message, event_name=name, fix=fix, priority="normal")
            self._weekly_review()

        _threading.Thread(target=work, name="pc-watcher", daemon=True).start()
        return []

    def _weekly_review(self) -> None:
        """Once a week: tell the user if there are fixes to review (by voice: 'review your mistakes')."""
        import time as _time


        store = SettingsStore(self.ctx.database)
        last = float(store.get("weekly_review_at", 0) or 0)
        if _time.time() - last < 7 * 24 * 3600:
            return
        store.set("weekly_review_at", _time.time())
        if last == 0:
            return  # the first week starts now
        try:
            from app.agent import learning

            counts = learning.summary(self.ctx.database)
        except Exception:
            return
        if counts.get("not_found") or counts.get("bad_answer") or counts.get("cancelled"):
            self.notice.emit("Weekly review ready: say 'review your mistakes' and I'll suggest fixes.")

    def _notify(self, title: str, message: str) -> None:
        """Reminders and automation results: shown in the chat and tray, into the notification center, and spoken
        unless the mode keeps things quiet."""
        from datetime import datetime

        from app.core import events, modes, proactive

        self.reminder.emit(title, message)
        try:
            events.BUS.publish(events.REMINDER, title=title, message=message)
        except Exception:
            _log.warning("could not publish the reminder", exc_info=True)
        if modes.STATE.speaks("high") and proactive.reminder_may_speak(self.ctx.settings, datetime.now()):
            self.tts.speak(message)  # in quiet hours it is shown and kept in the notification center, not spoken
