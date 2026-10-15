"""Health report, model comparison, speed report, voice training.

Part of AssistantController (app/ui/controller.py), split out in 3.4.1; the methods use its attributes
and signals through ``self``."""

from __future__ import annotations

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("ui.controller")


class HealthMixin:
    def health_report(self) -> None:
        """Voice / Menu: the full health report, in the background (about two minutes); the summary is spoken."""
        self.notice.emit("Making the health report: self-test, voice benchmark, Bangla answers, speed and mistakes "
                         "(about two minutes)...")
        self._run_health(weekly=False)

    def weekly_health_check(self, now=None) -> bool:
        """Housekeeping: once a week, while you're away, check everything; speak only if something got worse."""
        import time as _time

        from app import health

        if (not getattr(self.ctx.settings, "weekly_health_check", True) or self._health_running or self._busy
                or self._focus is not None):
            return False
        if _time.monotonic() - self._last_activity < self.WEEKLY_IDLE_MINUTES * 60:
            return False
        if not health.due(self.ctx.paths.data_dir, self.WEEKLY_DAYS, now):
            return False
        log_event(_log, "weekly_health_check")
        self._run_health(weekly=True)
        return True

    def _run_health(self, weekly: bool) -> None:
        import threading as _threading

        from app import health

        was_on = self.hands_free is not None
        self._health_running = True

        def work() -> None:
            try:
                out = (lambda _line: None) if weekly else (lambda line: self.notice.emit(line))
                path, summary = health.collect(self.ctx, self.ai, self.stt, self.tts.engine,
                                               lambda coro: self.runner.run(coro, timeout=900), out=out)
                bad = health.worse(summary)
                better = sum("(better" in line for line in summary)
                if weekly:
                    if bad:
                        self.notice.emit("Weekly health check - worse than last week:\n" + "\n".join(bad)
                                         + f"\nFull report: {path}")
                        self.tts.speak(f"Weekly health check: {len(bad)} thing{'s' if len(bad) > 1 else ''} got "
                                       "worse. The details are on screen.")
                    else:
                        self.notice.emit(f"Weekly health check: nothing got worse. Report: {path}")
                    return
                self.notice.emit("Health report:\n" + "\n".join(summary) + f"\nSaved: {path}")
                self.tts.speak(f"The health report is ready. {better} things got better and {len(bad)} got worse "
                               "since last time." if better or bad else "The health report is ready.")
            except Exception as exc:
                self.notice.emit(f"The health report stopped: {exc}")
            finally:
                self._health_running = False
                if was_on and self.hands_free is None:
                    self._restart_hands_free.emit()

        _threading.Thread(target=work, name="health-report", daemon=True).start()

    def compare_models(self) -> None:
        """Voice: "compare models" - the same Bangla questions for each installed chat model; the winner is spoken.
        It only reports; switching Jarvis to the winner stays a separate, explicit step."""
        import threading as _threading

        from app import model_compare

        self.notice.emit("Comparing your AI models on the Bangla question set (a few minutes per model)...")

        def work() -> None:
            try:
                results = self.runner.run(
                    model_compare.compare_installed(self.ctx.settings, out=lambda line: self.notice.emit(line)),
                    timeout=3600)
                winner = model_compare.pick_winner(results)
                if winner is None:
                    self.tts.speak("I found no chat models to compare.")
                    return
                self._best_model = winner.model
                self.tts.speak(f"The best model for you is {winner.model}, with {winner.score:.0f} percent. "
                               "Say: use the best model, to switch to it.")
            except Exception as exc:
                self.notice.emit(f"The model comparison stopped: {exc}")

        _threading.Thread(target=work, name="compare-models", daemon=True).start()

    def use_best_model(self) -> None:
        """Voice: "use the best model" - make "jarvis" from the comparison winner and switch to it.
        No model is deleted by voice (that stays Menu -> Make the Jarvis model, which lists what goes)."""
        import threading as _threading

        from app import model_compare

        if self._best_model:
            self.tts.speak(f"Switching to {self._best_model}.")
            self.make_jarvis_model(self._best_model, [])
            return
        self.notice.emit("First I'll compare your models to find the best one (a few minutes per model)...")

        def work() -> None:
            try:
                results = self.runner.run(
                    model_compare.compare_installed(self.ctx.settings, out=lambda line: self.notice.emit(line)),
                    timeout=3600)
            except Exception as exc:
                self.notice.emit(f"The model comparison stopped: {exc}")
                return
            winner = model_compare.pick_winner(results)
            if winner is None:
                self.tts.speak("I found no chat models to compare.")
                return
            self._best_model = winner.model
            self.tts.speak(f"The best model is {winner.model}. Switching to it.")
            self.make_jarvis_model(winner.model, [])

        _threading.Thread(target=work, name="use-best-model", daemon=True).start()

    def speed_report(self) -> None:
        """Menu -> Speed report."""
        report = self.speed_log.report()
        share = getattr(self.ai, "gpu_share", None)
        if share is not None:
            window = getattr(self.ai.provider, "context_window", None)
            report += (f"\n  AI model on the GPU: {share:.0%}" + (f" (context {window} tokens)" if window else "")
                       + ("" if share >= 0.97 or share == 0.0 else " - partly on the CPU, which is why it's slower"))
        self.notice.emit(report)

    def train_voice(self) -> None:
        """Menu -> Train my voice: learn how you pronounce your commands from the benchmark recordings."""
        import threading as _threading

        from app import benchmark
        from app.tools.applications import known_application

        if not benchmark.load_items(self.ctx.paths.data_dir):
            self.notice.emit("First record your commands once: run  jarvis-cli --record-benchmark  (about 3 minutes), "
                             "then Menu -> Train my voice.")
            return
        language = None if self.ctx.settings.voice_language == "auto" else self.ctx.settings.voice_language

        def work() -> None:
            try:
                self.notice.emit("Learning how you say your commands... (about a minute)")
                result = benchmark.train(self.ctx.paths.data_dir, lambda audio: self.stt.transcribe(audio, language),
                                         lambda name: known_application(name, self.executor.context),
                                         out=lambda line: self.notice.emit(line) if line.strip() else None)
                if not result:
                    self.notice.emit("No usable recordings: run  jarvis-cli --record-benchmark  again.")
            except Exception as exc:
                self.notice.emit(f"Voice training stopped: {exc}")
            finally:
                self.conversation._corrections_store = None  # use what was learned right away
                self.refresh_hints()

        _threading.Thread(target=work, name="voice-training", daemon=True).start()
