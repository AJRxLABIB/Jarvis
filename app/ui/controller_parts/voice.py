"""Listening: push-to-talk, hands-free, confirmations by voice, wake word and voice enrollment, barge-in, model warm-up.

Part of AssistantController (app/ui/controller.py), split out in 3.4.1; the methods use its attributes
and signals through ``self``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from functools import partial
import threading

from PySide6.QtCore import Slot

from app.agent.conversation import AssistantReply
from app.voice.pipeline import ListenResult
from app.voice.vad import create_vad
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("ui.controller")

if TYPE_CHECKING:
    from app.ui.controller import PendingConfirmation  # noqa: F401


class VoiceMixin:
    @Slot()
    def toggle_listening(self) -> None:
        if self.hands_free is not None and self.hands_free.state in ("command", "follow_up"):
            self.hands_free.cancel()
            return
        if self._listening:
            self.stop_listening()
        else:
            self.start_listening()

    @Slot()
    def start_listening(self) -> None:
        if self.hands_free is not None and self.hands_free.running:
            if not self._busy:
                self.tts.stop()
                self.hands_free.trigger()  # already listening: skip the wake phrase
            return
        if self._busy or self._listening:
            return
        if getattr(self, "mic_muted", False):
            self.notice.emit("The microphone is muted. Unmute it first (Ctrl+M, or Menu -> Mute microphone).")
            return
        if not self.ctx.settings.microphone_enabled:
            self.notice.emit("The microphone is turned off in settings (MICROPHONE_ENABLED=false).")
            return
        self.tts.stop()
        self._listening = True
        self._stop_listening = threading.Event()
        self._set_state("LISTENING")
        stop = self._stop_listening

        def work() -> None:
            try:
                result = self.pipeline.listen(stop)
            except Exception as exc:  # never let a recording thread die silently
                _log.exception("listening failed")
                result = ListenResult("error", message=f"Listening failed: {exc}")
            self._listen_done.emit(result)

        threading.Thread(target=work, name="listen", daemon=True).start()

    @Slot()
    def stop_listening(self) -> None:
        if self._stop_listening is not None:
            self._stop_listening.set()
        if self.hands_free is not None:
            self.hands_free.cancel()

    @Slot(object)
    def _on_listen_done(self, result: ListenResult) -> None:
        self._listening = False
        self._stop_listening = None
        heard = result.transcript.text if result.transcript else ""
        self._last_heard = heard or None
        if not result.ok:
            if heard:
                self.user_message.emit(heard)
            self.notice.emit(result.message)
            if result.status in ("wake_only", "low_confidence"):
                self.tts.speak(result.message)
            self._set_state("ERROR" if result.status == "error" else self._idle_state())
            return
        self._observe_tone(result.recording.audio if result.recording is not None else None, result.text)
        self.send_text(result.text, spoken=True)

    def _hands_free_paused(self) -> bool:
        """Don't listen to ourselves, and don't take commands while one is running or a question is open."""
        return self._busy or self._listening or self.tts.is_speaking or self._pending_confirmation is not None

    def ask_by_voice(self, pending: "PendingConfirmation") -> None:
        """Read the question out and accept a spoken "yes"/"no" (হ্যাঁ/না). The dialog stays up too."""
        listener = self.hands_free
        if listener is None or not self.ctx.settings.voice_confirm:
            return
        from app.agent.language import classify_answer, detect_language

        bangla = self.conversation.reply_language == "bn" or detect_language(pending.heard or "") == "bn"
        question = pending.request.summary
        self.tts.speak(f"{question} হ্যাঁ বা না বলুন।" if bangla else f"{question} Say yes or no.")

        def on_answer(text: str) -> bool:
            answer = classify_answer(text)
            if answer is None:
                return False
            if not pending.future.done():
                pending.future.set_result(answer)
                log_event(_log, "confirmed_by_voice", approved=answer)
                self.confirmation_answered.emit(pending, answer)
            return True

        listener.ask(on_answer)

    def _spoken_text(self) -> str:
        return self.tts.current_text if self.tts.is_speaking else ""

    def _other_audio(self):
        """Watches other apps' sound (a video, music) so the screen's voices aren't taken for yours."""
        if getattr(self, "_other_audio_watch", None) is None:
            from app.voice.other_audio import OtherAudio

            self._other_audio_watch = OtherAudio()
            self._other_audio_watch.start()
        return self._other_audio_watch

    def _voiceprint_tip(self) -> None:
        """The strongest protection against other voices is your voice print; say so once if it's missing."""
        s = self.ctx.settings
        try:
            verifier = self.create_speaker_verifier() if s.speaker_verification else None
            enrolled = bool(verifier is not None and verifier.enrolled)
        except Exception:
            enrolled = False
        if not enrolled and not getattr(self, "_voiceprint_tip_shown", False):
            self._voiceprint_tip_shown = True
            self.notice.emit("Tip: Menu > Enroll my voice (30 seconds). Then I only listen to YOU - not to videos, "
                             "the TV or my own voice from the speakers.")

    def _usual_phrases(self) -> list[str]:
        """Short replies said again and again: made ahead of time so they start instantly."""
        from app.voice.hands_free import dismissal_reply, wake_greeting

        s = self.ctx.settings
        address = s.user_address.strip()
        return [wake_greeting(s), dismissal_reply(s), "Done.", "Okay.", f"Yes, {address}." if address else "Yes.",
                "One moment.", "Sorry, I didn't catch that."]

    def _hold_speech(self) -> bool:
        """Hands-free thread: you started talking while Jarvis speaks - stop the sound now, keep the rest."""
        if self._pending_confirmation is not None:
            return False
        return self.tts.hold()

    def _release_speech(self, resume: bool) -> None:
        if resume:
            self.tts.resume_held()
        else:
            self.tts.discard_held()

    def _recent_speech(self) -> str:
        """The answer being spoken (all of it, not just the current sentence), and the last one for a few seconds
        after it ended: to recognise its own voice coming back through the microphone."""
        import time as _time

        parts = [self.tts.current_text if self.tts.is_speaking else "", getattr(self, "_reply_so_far", ""),
                 getattr(self.tts, "held_text", "")]
        last, at = getattr(self, "_last_reply", ("", 0.0))
        if self.tts.is_speaking or _time.monotonic() - at < self.ECHO_SECONDS:
            parts.append(last)
        return " ".join(p for p in parts if p)

    def _can_duplex(self) -> bool:
        s = self.ctx.settings
        return (s.voice_interrupt and self.tts.is_speaking and self._pending_confirmation is None
                and not self._listening and getattr(self.stt, "device", "cpu") == "cuda")

    def create_speaker_verifier(self):
        from app.voice.speaker import SpeakerVerifier

        return SpeakerVerifier(self.ctx.paths.models_dir / "speaker", self.ctx.paths.data_dir,
                               threshold=self.ctx.settings.speaker_threshold)

    def _wake_check(self):
        if not self.ctx.settings.personal_wake_check:
            return None
        from app.voice.wake_templates import WakeTemplates

        templates = WakeTemplates(self.ctx.paths.data_dir)
        return templates if templates.ready else None

    def teach_wake_word(self, times: int = 5) -> None:
        """Menu -> Teach me my wake word: record the wake phrase a few times (only features are kept)."""
        import threading as _threading

        from app.voice.wake_templates import WakeTemplates

        phrase = self.ctx.settings.wake_word.title()
        was_on = self.hands_free is not None
        self.stop_hands_free()

        def work() -> None:
            recordings = []
            try:
                for i in range(1, times + 1):
                    self.notice.emit(f"Wake word {i}/{times}: say \"{phrase}\" the way you normally do.")
                    self.tts.speak(f"Please say {phrase}." if i == 1 else "Again.")
                    self.tts.wait_until_idle(15)
                    recording = self.microphone.record(silence_timeout=0.7, start_timeout=6, max_seconds=3,
                                                       vad=create_vad(self.ctx.settings.vad_engine))
                    if recording.speech_detected:
                        recordings.append(recording.audio)
                WakeTemplates(self.ctx.paths.data_dir).enroll(recordings)
                self.notice.emit(f"Learned your \"{phrase}\" ({len(recordings)} recordings). Wake-ups that don't "
                                 "sound like it are now ignored (PERSONAL_WAKE_CHECK).")
                self.tts.speak("Got it.")
            except Exception as exc:  # reported to the user
                _log.warning("wake word enrollment failed", exc_info=True)
                self.notice.emit(f"Could not learn the wake word: {exc}")
            finally:
                if was_on:
                    self._restart_hands_free.emit()

        _threading.Thread(target=work, name="wake-enroll", daemon=True).start()

    def enroll_voice(self, phrases: int = 4) -> None:
        """Record a few sentences of the owner's voice (in the background) and save the voice print."""
        import threading as _threading

        from app.voice.speaker import SpeakerError, download_model

        prompts = ("Hey Jarvis, open Google Chrome and play some music.",
                   "What is the weather like today, and do I have any reminders?",
                   "জার্ভিস, আজকের খবর শোনাও আর ভলিউম একটু বাড়াও।",
                   "Please lock my computer when I leave the room.")
        was_on = self.hands_free is not None
        self.stop_hands_free()

        def work() -> None:
            recordings = []
            try:
                self.notice.emit("Voice enrollment: downloading the voice-print model (27 MB, once)...")
                download_model(self.ctx.paths.models_dir / "speaker")
                for i, sentence in enumerate(prompts[:phrases], 1):
                    self.notice.emit(f"Voice enrollment {i}/{phrases}: after the beep, say: \"{sentence}\"")
                    self.tts.speak(f"Please say: {sentence}" if i == 1 else "Next one.")
                    self.tts.wait_until_idle(20)
                    recording = self.microphone.record(silence_timeout=1.2, start_timeout=8, max_seconds=12)
                    if recording.speech_detected:
                        recordings.append(recording.audio)
                self.create_speaker_verifier().enroll(recordings)
                self.notice.emit("Your voice is enrolled. Turn on 'Only my voice' in Settings -> Voice input "
                                 "(SPEAKER_VERIFICATION=true) to ignore other voices.")
                self.tts.speak("Thank you. I will know your voice now.")
            except (SpeakerError, Exception) as exc:  # noqa: BLE001 - reported to the user
                _log.warning("voice enrollment failed", exc_info=True)
                self.notice.emit(f"Voice enrollment failed: {exc}")
            finally:
                if was_on:
                    self._restart_hands_free.emit()

        _threading.Thread(target=work, name="voice-enroll", daemon=True).start()

    def _hands_free_state_changed(self, state: str) -> None:
        capturing = state in ("command", "follow_up")
        self.microphone_active.emit(capturing)  # MICROPHONE ACTIVE while a command is being recorded
        if capturing:
            self._set_state_from_any_thread("LISTENING")
        elif state == "waiting" and self._state == "LISTENING":
            self._set_state_from_any_thread(self._idle_state())
        self.hands_free_state.emit(state)

    def _can_barge_in(self) -> bool:
        return (self.ctx.settings.barge_in and self.tts.is_speaking and self._pending_confirmation is None
                and not self._listening)

    def _barge_in(self) -> bool:
        """Called from the hands-free thread: "Hey Jarvis" while speaking. Stop talking (and a running
        request), so the new command can be heard. Never answers a pending confirmation."""
        if self._pending_confirmation is not None:
            return False
        self.tts.stop()
        future = self._future
        if future is not None and not future.done():
            future.cancel()
        log_event(_log, "barge_in")
        return True

    def _report_gpu_fit(self) -> None:
        share = getattr(self.ai, "gpu_share", None)
        if share is None or share >= 0.97 or share == 0.0 or self._gpu_fit_reported:
            return
        self._gpu_fit_reported = True
        self.notice.emit(
            f"Only {share:.0%} of the AI model fits in GPU memory, even with a smaller context, so answers are "
            "slower. It helps to close apps that use the GPU (games, video editors, many Chrome tabs), to let "
            "Jarvis start Ollama itself (quit Ollama from its tray icon: Jarvis then starts it with the "
            "memory-saving settings), or to use a smaller model.")

    def _mic_changed(self, on: bool) -> None:
        if on and not self._mic_on:
            self.voice_levels.clear()       # a new utterance starts
        self._mic_on = on

    def _collect_level(self, dbfs: float) -> None:
        if self._mic_on:
            self.voice_levels.append(max(0.0, min(1.0, (dbfs + 60.0) / 50.0)))

    def spoken_wave(self) -> list[float] | None:
        """The microphone levels of the message just sent, if it was spoken (leading/trailing quiet trimmed)."""
        if not self.last_message_spoken:
            return None
        levels = list(self.voice_levels)
        while levels and levels[0] < 0.08:
            levels.pop(0)
        while levels and levels[-1] < 0.08:
            levels.pop()
        return levels or None

    def warm_up_model(self) -> None:
        """Load the AI model in the background, so the next answer starts without the load delay."""
        import time as _time

        now = _time.monotonic()
        if now - self._last_warm_up < self.WARM_UP_EVERY:
            return
        self._last_warm_up = now
        self._model_unloaded = False
        self._last_activity = now
        try:
            future = self.runner.submit(self.ai.warm_up())
            future.add_done_callback(lambda _f: self._gpu_fit_checked.emit())
        except Exception:
            _log.warning("could not start the model warm-up", exc_info=True)
        if self.ctx.settings.home_assistant_url and self.ctx.settings.home_assistant_token:
            import threading as _threading

            from app.tools.smart_home import prefetch

            _threading.Thread(target=prefetch, args=(self.executor.context,), name="home-devices", daemon=True).start()

    def _hands_free_woke(self) -> None:
        self.warm_up_model()  # "Jarvis..." -> load the model while the command is still being spoken
        if self.ctx.settings.wake_sound:
            from app.tts.base import AudioClip
            from app.voice.hands_free import wake_chime

            self.tts.play_sound(AudioClip(wake_chime(), 22050))
        self.tts.stop()

    def start_hands_free(self) -> bool:
        """Start always-listening if HANDS_FREE is on. Returns whether it is running."""
        s = self.ctx.settings
        if self.notes is not None:
            return False  # the notes have the microphone; hands-free comes back when they end
        if not s.hands_free or self.hands_free is not None:
            return self.hands_free is not None
        if getattr(self, "mic_muted", False):
            return False                                   # muted: nothing listens until you unmute
        if not s.microphone_enabled:
            self.notice.emit("Hands-free listening needs the microphone (MICROPHONE_ENABLED=true).")
            return False
        from app.voice.hands_free import HandsFreeListener, prepare_detector, resolve_engine

        engine = resolve_engine(s)
        model_dir = self.ctx.paths.models_dir / "wakeword"
        factory = None
        if engine == "openwakeword":
            factory = partial(prepare_detector, s, model_dir, allow_download=not s.forced_offline,
                              notice=self.notice.emit)
        self.hands_free = HandsFreeListener(
            s, self.microphone, self.stt, detector_factory=factory,
            on_result=self._hands_free_result.emit, on_state=self._hands_free_state_changed,
            on_notice=self.notice.emit, on_wake=self._hands_free_woke, should_pause=self._hands_free_paused,
            on_level=self.level_changed.emit, vad=create_vad(s.vad_engine),
            can_barge_in=self._can_barge_in, on_barge_in=self._barge_in,
            speaker=self.create_speaker_verifier() if s.speaker_verification else None,
            spoken_text=self._spoken_text, can_duplex=self._can_duplex, on_partial=self.partial_transcript.emit,
            recent_speech=self._recent_speech,
            wake_check=self._wake_check(),
            playback_level=self.tts.playback_db, on_hold=self._hold_speech, on_release=self._release_speech,
            other_audio=self._other_audio(),
            on_transcribing=self.transcribing.emit,
        )
        self.hands_free.start()
        self._voiceprint_tip()
        wake = s.wake_word.title()
        self.notice.emit(f"Hands-free is on: say \"{wake}\" any time." + (
            "" if engine == "openwakeword" else " (Using speech recognition to spot the wake phrase: more CPU.)"))
        log_event(_log, "hands_free_started", engine=engine)
        return True

    mic_muted = False

    def set_mic_muted(self, muted: bool) -> str:
        """The microphone mute: when on, NOTHING listens - no wake word, no push-to-talk, no hands-free. The
        microphone stream is closed (not just ignored), so the system's own mic indicator goes off too."""
        muted = bool(muted)
        if muted == self.mic_muted:
            return "The microphone is already " + ("muted." if muted else "on.")
        self.mic_muted = muted
        if muted:
            self.stop_listening()
            self.stop_hands_free()
            self.microphone_active.emit(False)
            message = "Microphone muted: I can't hear anything until you unmute it (Ctrl+M or Menu)."
        else:
            running = self.start_hands_free()
            message = "Microphone on." + (" Hands-free is listening again." if running else " Press Talk to speak.")
        self.mic_muted_changed.emit(muted)
        self.notice.emit(message)
        log_event(_log, "microphone_muted" if muted else "microphone_unmuted")
        return message

    def stop_hands_free(self) -> None:
        listener, self.hands_free = self.hands_free, None
        if listener is not None:
            listener.stop()
            self.microphone_active.emit(False)
            self.hands_free_state.emit("stopped")
            log_event(_log, "hands_free_stopped")

    def set_hands_free(self, enabled: bool, *, persist: bool = True) -> None:
        """Tray / menu toggle. Saved to .env so it stays on after a restart."""
        settings = self.ctx.settings.model_copy(update={"hands_free": enabled})
        if persist:
            from app.config.env_writer import update_env_file
            from app.config.settings import ConfigError

            try:
                settings = update_env_file(self.ctx.paths.env_file, {"HANDS_FREE": "true" if enabled else "false"})
            except (ConfigError, OSError) as exc:
                self.notice.emit(f"Could not save the setting: {exc}")
        self.ctx.settings = settings
        self.stop_hands_free()
        if enabled:
            self.start_hands_free()
        else:
            self.notice.emit("Hands-free is off. Press Talk (or the hotkey) to speak.")

    def _record_voice(self, result: ListenResult) -> int | None:
        """One small measurement row per thing heard (no audio, no words). Never allowed to disturb listening."""
        try:
            if self.ctx.database is None:
                return None
            from app.core.voice_stats import VoiceStats

            t = result.transcript
            return VoiceStats(self.ctx.database).record(
                result.status, language=getattr(t, "language", None), confidence=getattr(t, "confidence", None),
                stt_ms=getattr(t, "elapsed_ms", None))
        except Exception:
            _log.warning("could not record the voice measurement", exc_info=True)
            return None

    @Slot(object)
    def _on_hands_free_result(self, result: ListenResult) -> None:
        listener = self.hands_free
        self._timing = {"heard": result.transcript.elapsed_ms / 1000 if result.transcript else None}
        self._timing["voice_event"] = self._record_voice(result)
        heard = result.transcript.text if result.transcript else ""
        self._last_heard = heard or None
        if result.status in ("wake_only", "dismissed"):
            # "Jarvis?" -> "Yes, sir! How can I help you?" and keep listening; "goodbye" -> back to the wake word
            if heard:
                self.user_message.emit(heard)
            self.assistant_finished.emit(AssistantReply(result.message, ok=True))
            self.tts.stop()
            self.tts.speak(result.message)
            if listener is not None:
                listener.resume(follow_up=result.status == "wake_only")
            return
        if not result.ok:
            if heard:
                self.user_message.emit(heard)
            self.notice.emit(result.message)
            self.tts.speak(result.message)
            if listener is not None:
                listener.resume(follow_up=True)
            return
        if self.dictating:  # dictation: type it instead of answering
            self.user_message.emit(result.text)
            still = self._dictate(result.text)
            if listener is not None:
                listener.resume(follow_up=still)
            return
        self._observe_tone(result.audio, result.text)
        if not self.send_text(result.text, spoken=True) and listener is not None:
            listener.resume(follow_up=False)
