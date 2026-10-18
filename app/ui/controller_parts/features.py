"""Jarvis features the window asks for: updates, self-test, meeting notes, hints, the Jarvis model, settings and dictation by voice.

Part of AssistantController (app/ui/controller.py), split out in 3.4.1; the methods use its attributes
and signals through ``self``."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtCore import Slot

from app.voice.vad import create_vad
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("ui.controller")


class FeaturesMixin:
    def _emit_sources(self, reply) -> None:
        """Under an answer that used tools or saved memories: where its facts came from (SHOW_SOURCES)."""
        if not getattr(reply, "sources", None) or not getattr(self.ctx.settings, "show_sources", True):
            return
        from app.agent.conversation import sources_line

        line = sources_line(reply.sources)
        if line:
            self.notice.emit(line)

    def check_for_updates(self, manual: bool = False) -> None:
        """UPDATE_MANIFEST_URL: is a newer version published? (in the background; never installs by itself)"""
        url = self.ctx.settings.update_manifest_url.strip()
        if not url:
            if manual:
                self.notice.emit("Update checks are off: set UPDATE_MANIFEST_URL in Settings to where you publish "
                                 "releases.")
            return
        import threading as _threading

        from app.utils import updates

        def work() -> None:
            try:
                info = updates.check(url)
            except updates.UpdateError as exc:
                if manual:
                    self.notice.emit(str(exc))
                return
            if info is None:
                if manual:
                    self.notice.emit("You have the latest version.")
                return
            self._update_found.emit(info)

        _threading.Thread(target=work, name="update-check", daemon=True).start()

    def run_self_test(self) -> None:
        """Menu -> Run self-test: every feature tried for real; results appear in the chat."""
        import threading as _threading

        from app.selftest import Parts, run_selftest, summary

        was_on = self.hands_free is not None
        self.stop_hands_free()  # the microphone test needs the microphone

        def work() -> None:
            try:
                parts = Parts(self.ctx.settings, ai=self.ai, stt=self.stt, tts_engine=self.tts.engine,
                              microphone=self.microphone, models_dir=self.ctx.paths.models_dir,
                              run_async=lambda coro: self.runner.run(coro, timeout=120))
                results = run_selftest(parts, on_result=lambda r: self.notice.emit(r.line()))
                self.notice.emit("Self-test: " + summary(results))
            except Exception as exc:
                self.notice.emit(f"The self-test stopped: {exc}")
            finally:
                if was_on:
                    self._restart_hands_free.emit()

        _threading.Thread(target=work, name="self-test", daemon=True).start()

    def _notes_control(self, action: str) -> str:
        """The meeting_notes tool (runs on the assistant's thread): the work happens on the window's thread."""
        running = self.notes is not None
        if action == "status":
            return (f"I'm taking notes: {len(self.notes.lines)} sentence(s) so far." if running
                    else "I'm not taking notes right now.")
        if action == "start":
            if running:
                return "I'm already taking notes."
            if not self.ctx.settings.microphone_enabled:
                from app.tools.base import ToolError

                raise ToolError("The microphone is turned off in Settings.", "microphone_off")
            self._notes_request.emit("start")
            return ("Taking notes in a moment. Say \"Jarvis, stop the notes\" when it's over, and I'll write "
                    "a summary.")
        if not running:
            return "I'm not taking notes."
        self._notes_request.emit("stop")
        return "Stopping the notes; I'll write the summary now."

    def toggle_notes(self) -> None:
        """Menu -> Meeting / lecture notes."""
        self._notes_action("stop" if self.notes is not None else "now")

    @Slot(str)
    def _notes_action(self, action: str) -> None:
        if action == "start":
            QTimer.singleShot(2500, self._start_notes)  # let the spoken reply finish first
        elif action == "now":
            self._start_notes()
        else:
            self._finish_notes()

    def _start_notes(self) -> None:
        if self.notes is not None:
            return
        from app.voice.meeting import MeetingNotes, notes_folder

        s = self.ctx.settings
        language = None if s.voice_language == "auto" else s.voice_language
        self._notes_resume_hands_free = self.hands_free is not None
        self.stop_hands_free()
        notes = MeetingNotes(self.microphone, lambda audio: self.stt.transcribe(audio, language).text, notes_folder(),
                             vad_factory=lambda: create_vad(s.vad_engine), on_line=self.notes_line.emit,
                             on_stop_request=lambda: self._notes_request.emit("stop"))
        self.notes = notes
        try:
            path = notes.start()
        except Exception as exc:
            self.notes = None
            self.notice.emit(f"The notes could not start: {exc}")
            if self._notes_resume_hands_free:
                self.start_hands_free()
            return
        self.notes_active.emit(True)
        self.notice.emit(f"Taking notes (written as we go to {path.name} in {path.parent}). Say \"Jarvis, stop the "
                         "notes\" or use Menu -> Meeting / lecture notes to finish. Audio is not saved.")

    def _finish_notes(self) -> None:
        notes, self.notes = self.notes, None
        if notes is None:
            return
        import threading as _threading

        def work() -> None:
            from app.ai.base import ChatMessage
            from app.ai.ollama import strip_think
            from app.voice.meeting import save_notes, summarize

            try:
                path = notes.stop()
                self.notes_active.emit(False)
                if path is None:
                    return
                self.notice.emit(f"Notes stopped: {len(notes.lines)} sentence(s). Writing the summary...")

                def ask(prompt: str) -> str:
                    reply = self.runner.run(self.ai.chat([ChatMessage.user(prompt)]), timeout=600)
                    return strip_think(reply.message.content or "")

                try:
                    summary = summarize(notes.lines, ask)
                except Exception as exc:
                    summary = f"(No summary: the AI model was not available - {exc})"
                started = notes.started_at
                title = f"Notes {started:%d %B %Y, %H:%M}" if started else "Notes"
                document = save_notes(path, title, summary, notes.lines)
                self.notice.emit(f"Notes saved: {document}")
                log_event(_log, "notes_saved", lines=len(notes.lines))
                from app.tools import winapi

                if winapi.is_windows():
                    try:
                        winapi.start_file(str(document))
                    except Exception:
                        pass
            except Exception as exc:
                self.notice.emit(f"The notes could not be finished: {exc}")
            finally:
                self.notes_active.emit(False)
                if self._notes_resume_hands_free:
                    self._restart_hands_free.emit()

        _threading.Thread(target=work, name="notes-finish", daemon=True).start()

    def _sort_downloads(self) -> None:
        """AUTO_SORT_DOWNLOADS: every 5 minutes, new files in Downloads go into folders by type."""
        if not self.ctx.settings.auto_sort_downloads:
            self._downloads_timer.stop()
            return
        import threading as _threading

        from app.tools.housekeeping import describe_counts, sort_downloads

        def work() -> None:
            try:
                counts = sort_downloads(self.executor.context)
            except Exception:
                _log.warning("sorting Downloads failed", exc_info=True)
                return
            if counts:
                self.notice.emit(f"Sorted {sum(counts.values())} new download(s) into folders "
                                 f"({describe_counts(counts)}). Say 'undo sort downloads' to put them back.")

        _threading.Thread(target=work, name="sort-downloads", daemon=True).start()

    def _refresh_hints_now_and_then(self) -> None:
        """New nicknames, corrections and contacts reach the recognizer within half an hour, without a restart."""
        import time as _time

        if _time.monotonic() - self._hints_at >= self.HINTS_EVERY:
            self._hints_at = _time.monotonic()
            import threading as _threading

            # reads nicknames, the database and the (encrypted) contacts: not on the window's thread
            _threading.Thread(target=self.refresh_hints, name="recognition-hints", daemon=True).start()

    def refresh_hints(self) -> None:
        """Your words (CUSTOM_VOCABULARY, learned corrections) as hints for the speech recognizer, right away."""
        if not hasattr(self.stt, "hotwords"):
            return
        try:
            from app.cli_voice import recognition_hints, vocabulary

            s = self.ctx.settings
            words = vocabulary(self.ctx)
            self.stt.hotwords = recognition_hints(s.wake_word, s.assistant_name, words) or None
            from app.voice.speech_dataset import bangla_hints

            self.stt.hotwords_bn = bangla_hints(words)
        except Exception:
            _log.warning("could not refresh the recognition hints", exc_info=True)

    def _live_control(self, text: str) -> bool:
        """'stop speaking' / 'pause this mission' / 'cancel the current task' / 'explain what you are doing' /
        'show me the plan' / "don't remember this": handled at once, even while a request runs (live_control.py)."""
        from app.core import live_control

        intent = live_control.classify(text)
        if intent is None or (not self._busy and intent not in ("stop_speaking", "dont_remember")):
            return False                        # idle: the ordinary shortcuts / tools handle the rest
        self.user_message.emit(text)
        mission = self._running_mission()
        if intent == "stop_speaking":
            self.tts.stop()
            message = "Stopped speaking." + (" The request is still running - say 'cancel the current task' to stop it."
                                              if self._busy else "")
        elif intent == "dont_remember":
            message = self._mode_control("private") + " Nothing from now on is saved; say 'normal mode' to end it."
        elif intent in ("pause_mission", "cancel_task") and mission is not None:
            from app.core import missions

            kind = "pause" if intent == "pause_mission" else "cancel"
            missions.request(mission.id, kind)
            message = (f"{'Pausing' if kind == 'pause' else 'Cancelling'} '{mission.title}' before its next step "
                       "(the step that is running now finishes first; nothing is cut off half-way).")
        elif intent == "cancel_task":
            self.cancel()
            message = "Cancelled the current request."
        elif intent == "pause_mission":
            message = "No mission is running. (To stop the current request say 'cancel the current task'.)"
        elif intent == "show_plan" and mission is not None:
            from app.core.live_control import plan_text

            message = plan_text(mission) + "\nSay 'pause this mission' to stop before the next step."
        else:                                                     # explain_now, or show_plan without a mission
            state = {"PROCESSING": "thinking about your request", "EXECUTING": "running an action",
                     "WAITING_FOR_PERMISSION": "waiting for your answer to a question",
                     "SPEAKING": "speaking", "LISTENING": "listening"}.get(self._state, self._state.lower())
            message = f"I'm {state}."
            if mission is not None:
                step = next((s for s in mission.steps if s.state == "running"), None)
                message += (f" Mission '{mission.title}': step {step.n} of {len(mission.steps)}, {step.title}."
                            if step else f" Mission '{mission.title}' is {mission.state}.")
            current = getattr(self, "_current_request", "")
            if current:
                message += f" Your request: \"{current[:120]}\"."
        self.notice.emit(message)
        log_event(_log, "live_control", intent=intent, busy=self._busy)
        return True

    def live_privacy_state(self) -> dict:
        """The security center's "Right now": what really is on at this moment."""
        from app.core import modes

        settings = self.ctx.settings
        pending = getattr(self, "_pending_confirmation", None)
        screens = getattr(self, "_phone_screens", None)
        remote = getattr(self, "remote", None)
        return {"mic_muted": bool(getattr(self, "mic_muted", False)),
                "hands_free": getattr(self, "hands_free", None) is not None,
                "recording": bool(getattr(self, "_listening", False)),
                "screen_capture": bool(getattr(settings, "screen_capture_enabled", False)),
                "sharing_phones": [s.name for s in screens.active()] if screens is not None else [],
                "remote": getattr(remote, "url", "") if remote is not None else "",
                "pending_approval": (getattr(pending.request, "summary", "an action")
                                     if pending is not None and not pending.future.done() else ""),
                "mode": modes.STATE.current.name}

    def _running_mission(self):
        database = getattr(self.ctx, "database", None)
        if database is None:
            return None
        try:
            from app.core.missions import MissionStore

            found = MissionStore(database).recent(1, ("running", "verifying"))
            return found[0] if found else None
        except Exception:
            return None

    def _keep_voice(self, audio, transcript) -> None:
        """SPEECH_DATASET: remember the last voice command (for a correction) and keep it if mode is "all"."""
        mode = getattr(self.ctx.settings, "speech_dataset", "off")
        if mode == "off" or audio is None or transcript is None:
            self._last_voice = None
            return
        import time as _time

        language = getattr(transcript, "language", None) or "und"
        self._last_voice = (audio, transcript.text, language, _time.monotonic())
        if mode == "all" and getattr(transcript, "confidence", 0.0) >= 0.8:
            self._speech_dataset().record(audio, transcript.text, language, "unverified")

    def _speech_dataset(self):
        from app.voice.speech_dataset import SpeechDataset

        return SpeechDataset(self.ctx.paths.data_dir)

    def _on_correction(self, heard: str, corrected: str) -> None:
        """'no, I said ...': the misheard audio + the corrected text is the best training example there is."""
        last = getattr(self, "_last_voice", None)
        if last is None or getattr(self.ctx.settings, "speech_dataset", "off") == "off":
            return
        import time as _time

        from app.voice.speech_dataset import is_bangla

        audio, text, language, at = last
        if text.strip().lower() != heard.strip().lower() or _time.monotonic() - at > 180:
            return
        if is_bangla(corrected):
            language = "bn"
        self._speech_dataset().record(audio, corrected, language, "corrected")
        self._last_voice = None

    def _observe_tone(self, audio, text: str) -> None:
        """TONE_AWARE: in a hurry / tired -> a hint for the next answer's style."""
        self.conversation.tone_hint = ""
        if audio is None or not self.ctx.settings.tone_aware:
            return
        try:
            from app.voice.tone import HINTS, ToneTracker

            if getattr(self, "_tone", None) is None:
                self._tone = ToneTracker(self.ctx.paths.data_dir)
            tone = self._tone.observe(audio, text)
        except Exception:
            _log.warning("tone check failed", exc_info=True)
            return
        if tone:
            log_event(_log, "tone_noticed", tone=tone)
            self.conversation.tone_hint = HINTS[tone]

    def jarvis_model_plan(self):
        """(base model, cleanup plan) for Menu -> Make the Jarvis model (None base = no chat model installed)."""
        from app.ai import jarvis_model

        s = self.ctx.settings
        models = self.runner.run(self.ai.refresh_models(), timeout=30)
        base = jarvis_model.choose_base(models, s.ai_model)
        plan = jarvis_model.cleanup_plan(models, keep_names=(s.ai_fast_model, s.ai_smart_model, s.vision_model,
                                                             s.embedding_model, base.name if base else ""))
        return base, plan

    def make_jarvis_model(self, base: str, delete: list[str]) -> None:
        """Create "jarvis" from ``base``, switch to it, then delete ``delete`` (the user agreed to the list)."""
        from app.ai import jarvis_model

        async def work() -> None:
            self.notice.emit(f"Making the model \"jarvis\" from {base}...")
            await jarvis_model.build(self.ai, base, self.ctx.settings)
            try:
                from app.config.env_writer import update_env_file

                self.ctx.settings = update_env_file(self.ctx.paths.env_file, {"AI_MODEL": jarvis_model.JARVIS_MODEL})
            except Exception as exc:
                self.notice.emit(f"\"jarvis\" is ready, but AI_MODEL could not be saved: {exc}")
            removed = await jarvis_model.delete_models(self.ai, [n for n in delete if n != base]) if delete else []
            if delete and base in delete:  # the base's tag last: "jarvis" keeps its weights
                removed += await jarvis_model.delete_models(self.ai, [base])
            self.notice.emit("Jarvis now uses its own model \"jarvis\"." +
                             (f" Removed: {', '.join(removed)}." if removed else ""))
            self.refresh_status()

        future = self.runner.submit(work())
        future.add_done_callback(lambda f: f.exception() is not None and self.notice.emit(
            f"Making the Jarvis model failed: {f.exception()}"))

    def _jarvis_control(self, action: str) -> str:
        """jarvis_control tool (assistant thread): the window does it on its own thread."""
        self.jarvis_action.emit(action)
        return {"open_settings": "Opening my settings.", "open_memory": "Opening your memory.",
                "open_privacy": "Opening the privacy dashboard.", "open_logs": "Opening the logs.",
                "open_automations": "Opening your automations.", "mission_control": "Opening Mission Control.",
                "open_notifications": "Opening your notifications.", "emergency_controls": "Opening the emergency controls.",
                "evolution_center": "Opening the Evolution Center.", "task_center": "Opening the task center.", "my_day": "Opening your day.", "workflow_studio": "Opening the workflow studio.", "security_center": "Opening the security center.", "show_system": "Showing the system page.",
                "show_dashboard": "Showing the dashboard.", "enroll_voice": "Let's learn your voice.",
                "teach_wake_word": "Let's learn your wake word.", "train_voice": "Training on your recordings.",
                "make_jarvis_model": "Making the Jarvis model.", "self_test": "Running the self-test.",
                "speed_report": "Here is the speed report.", "check_updates": "Checking for Jarvis updates.",
                "phone_remote": "Starting the phone remote.", "new_conversation": "Starting a new conversation.",
                "hide_window": "Going to the background. Say my name any time.", "show_window": "Here I am.",
                "setup_wizard": "Opening the setup wizard.", "hands_free_off": "Hands-free is off.",
                "mute_microphone": "Muting the microphone. Press Ctrl+M to turn it back on.",
                "close_dialog": "Closed.", "use_best_model": "Switching to the best model.", "full_screen": "Full screen.", "stay_on_top": "I'll stay on top of other windows.",
                "not_on_top": "Other windows can go over me again.",
                "exit_full_screen": "Leaving full screen.", "health_report": "Making the health report.",
                "compare_models": "Comparing your models on the Bangla questions.",
                "restart_jarvis": "Restarting. I'll be back in a few seconds.",
                "window_overview": "Here are your open windows.", "tab_overview": "Here are your open tabs.",
                "phone_screen": "Opening your phone's screen. Start sharing on the phone if it isn't already.",
                "workspaces": "Here are your workspaces."}.get(action, "Done.")

    def _apply_setting(self, field: str, value: str) -> None:
        """change_setting tool: saved to .env and applied (on the window's thread)."""
        self._setting_request.emit(field, value)

    @Slot(str, str)
    def _apply_setting_now(self, field: str, value: str) -> None:
        from app.config.env_writer import update_env_file
        from app.config.settings import ENV_NAMES, ConfigError

        try:
            saved = update_env_file(self.ctx.paths.env_file, {ENV_NAMES[field]: value})
        except (ConfigError, OSError, KeyError) as exc:
            self.notice.emit(f"That setting could not be changed: {exc}")
            return
        self.apply_settings(saved, {field})
        self.ctx.settings = saved
        self.executor.context.settings = saved

    def _dictation_control(self, action: str) -> str:
        if action == "start":
            self.dictating = True
            self.notice.emit("Dictation on: everything you say is typed where the cursor is. Say \"stop dictation\" "
                             "to finish. (\"new line\", \"comma\", \"full stop\" work too.)")
            return "Dictation on. Speak, and I'll type."
        self.dictating = False
        return "Dictation off."

    def _dictate(self, text: str) -> bool:
        """While dictating: type what was said. Returns False when dictation just ended."""
        import re as _re

        cleaned = " ".join(text.split())
        if cleaned.lower().strip(" .!?।") in self._DICTATION_STOP:
            self.dictating = False
            self.notice.emit("Dictation off.")
            self.tts.speak("Dictation off.")
            return False
        for pattern, replacement in self._DICTATION_WORDS:
            cleaned = _re.sub(pattern, replacement, cleaned, flags=_re.I)
        from app.tools import winapi
        from app.utils.redaction import redact

        if redact(cleaned) != cleaned:
            self.notice.emit("Not typed: that looked like a password or card number.")
            return True
        try:
            window = winapi.target_window()
            if window is not None:
                from app.vision import capture as cap

                cap.check_not_blocked([window], cap.blocked_patterns(self.ctx.settings.screen_blocked_windows))
                if winapi.foreground_window() != window.hwnd:
                    winapi.focus_window(window.hwnd)
            winapi.type_unicode(cleaned + ("" if cleaned.endswith(("\n", " ")) else " "))
            self.events_typed = getattr(self, "events_typed", 0) + 1
        except Exception as exc:
            self.notice.emit(f"Could not type it: {exc}")
        return True
