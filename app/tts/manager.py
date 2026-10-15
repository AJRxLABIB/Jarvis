"""Speech output manager: engine selection, background speech queue, stop/mute.

``speak()`` never blocks the caller. A single worker thread synthesizes and
plays queued sentences in order. ``stop()`` interrupts the current sentence
and discards everything queued.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.config.settings import Settings
from app.tts.base import AudioClip, TTSEngine, TTSError
from app.tts.piper import PiperTTS
from app.tts.player import Player, SoundDevicePlayer
from app.tts.sapi import SapiTTS
from app.tts.text import clean_for_speech
from app.utils.logging_setup import get_logger, log_event
from app.voice.audio import AudioError

_log = get_logger("tts")


def create_engine(settings: Settings, voice_dir: Path) -> tuple[TTSEngine | None, str | None]:
    """Pick and load an engine. Returns (engine, problem message)."""
    candidates: list[TTSEngine] = []
    if settings.tts_engine == "jarvis":  # the JARVIS voice profile (app/tts/jarvis_voice.py)
        from app.tts.jarvis_voice import ENGLISH_VOICE, OFFLINE_VOICE, PITCH_OFFSET, RATE_OFFSET, JarvisVoice
        from app.tts.neural import NeuralTTS

        offline, problem = create_engine(settings.model_copy(update={"tts_engine": "auto",
                                                                     "tts_kokoro_voice": OFFLINE_VOICE}), voice_dir)
        neural = NeuralTTS(ENGLISH_VOICE, settings.tts_neural_voice_bn, fallback=offline,
                           rate_offset=RATE_OFFSET, pitch_offset=PITCH_OFFSET)
        try:
            neural.load()
            return JarvisVoice(neural), None
        except TTSError as exc:
            log_event(_log, "tts_engine_unavailable", engine="jarvis-neural", reason=str(exc))
            if offline is not None:
                return JarvisVoice(offline), None
            return None, problem or str(exc)
    if settings.tts_engine == "neural":
        from app.tts.neural import NeuralTTS

        offline, _ = create_engine(settings.model_copy(update={"tts_engine": "auto"}), voice_dir)
        candidates.append(NeuralTTS(settings.tts_neural_voice_en, settings.tts_neural_voice_bn, fallback=offline))
    if settings.tts_engine in ("kokoro", "auto"):
        from app.tts.kokoro import KokoroTTS, is_installed

        kokoro_dir = voice_dir.parent / "kokoro"
        if settings.tts_engine == "kokoro" or is_installed(kokoro_dir):  # "auto": only once it is downloaded
            candidates.append(KokoroTTS(kokoro_dir, settings.tts_kokoro_voice,
                                        allow_download=settings.tts_engine == "kokoro" and not settings.forced_offline))
    if settings.tts_engine in ("auto", "piper", "neural", "kokoro"):
        candidates.append(PiperTTS(settings.tts_voice, voice_dir))
    if settings.tts_engine in ("auto", "sapi", "neural"):
        candidates.append(SapiTTS(settings.tts_sapi_voice))
    problems: list[str] = []
    for engine in candidates:
        try:
            engine.load()
            if engine.name in ("piper", "sapi", "kokoro"):
                from app.tts import bangla

                if bangla.is_installed(voice_dir.parent):  # Bangla sentences get the offline Bangla voice
                    wrapped = bangla.BilingualTTS(engine, bangla.BanglaTTS(voice_dir.parent))
                    return wrapped, None
            return engine, None
        except TTSError as exc:
            problems.append(str(exc))
            log_event(_log, "tts_engine_unavailable", engine=engine.name, reason=str(exc))
    return None, " ".join(problems) or "No speech engine is available."


@dataclass
class _Item:
    text: str
    generation: int
    done: threading.Event


class TTSManager:
    def __init__(
        self,
        settings: Settings,
        voice_dir: Path,
        *,
        engine: TTSEngine | None = None,
        player: Player | None = None,
        on_speaking_change: Callable[[bool], None] | None = None,
        engine_factory: Callable[[], tuple[TTSEngine | None, str | None]] | None = None,
    ) -> None:
        self.enabled = settings.tts_enabled
        self.speed = settings.tts_speed
        self.volume = settings.tts_volume
        self.muted = False
        self.on_speaking_change = on_speaking_change
        self._player = player or SoundDevicePlayer(settings.output_device)
        self._engine = engine
        self._engine_factory = engine_factory or (lambda: create_engine(settings, voice_dir))
        self.problem: str | None = None
        self._queue: queue.Queue[_Item | None] = queue.Queue()
        self._generation = 0
        self._abort = threading.Event()
        self._speaking = False
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0
        self._state_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._ready = threading.Event()
        self.current_text = ""  # the sentence being played (to recognise our own voice in the microphone)

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="tts-worker", daemon=True)
            self._worker.start()

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Wait until the engine has been loaded (or failed to load)."""
        self.start()
        return self._ready.wait(timeout)

    @property
    def engine(self) -> TTSEngine | None:
        return self._engine

    @property
    def available(self) -> bool:
        return self._engine is not None

    def close(self) -> None:
        self.stop()
        if self._worker is not None:
            self._queue.put(None)
            self._worker.join(timeout=5)
            self._worker = None
        if self._engine is not None:
            self._engine.close()

    # ------------------------------------------------------------------ control

    @property
    def is_speaking(self) -> bool:
        return not self._idle.is_set()

    def speak(self, text: str) -> threading.Event:
        """Queue text. The returned event is set when it has been spoken or skipped."""
        done = threading.Event()
        text = clean_for_speech(text)
        if not text or not self.enabled or self.muted:
            done.set()
            return done
        self.start()
        with self._state_lock:
            self._pending += 1
            self._idle.clear()
            item = _Item(text, self._generation, done)
        self._queue.put(item)
        return done

    def stop(self) -> None:
        """Stop the current sentence and drop everything queued."""
        with self._state_lock:
            self._generation += 1
            self._abort.set()

    def play_sound(self, clip: "AudioClip") -> None:
        """A short interface sound (the wake chime). It bypasses the speech queue, so it
        neither interrupts speech nor counts as speaking (listening continues)."""
        if self.muted or clip.samples.size == 0:
            return

        def run() -> None:
            try:
                self._player.play(clip, volume=self.volume, abort=threading.Event())
            except Exception:
                _log.warning("could not play a sound", exc_info=True)

        threading.Thread(target=run, name="ui-sound", daemon=True).start()

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if muted:
            self.stop()

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    # ------------------------------------------------------------------- worker

    def _set_speaking(self, value: bool) -> None:
        if value == self._speaking:
            return
        self._speaking = value
        if self.on_speaking_change is not None:
            try:
                self.on_speaking_change(value)
            except Exception:
                _log.warning("speaking indicator callback failed", exc_info=True)

    def _finish(self, item: _Item) -> None:
        item.done.set()
        with self._state_lock:
            self._pending -= 1
            if self._pending == 0:
                # Become idle BEFORE notifying: a listener that checks is_speaking in
                # response (or concurrently) must already see False.
                self._idle.set()
                self._set_speaking(False)

    def _run(self) -> None:
        """Synthesis runs one sentence ahead of playback: while sentence 1 plays, sentence 2 is
        being generated (important for the online neural voice), so there's no gap between them."""
        if self._engine is None:
            try:
                self._engine, self.problem = self._engine_factory()
            except Exception as exc:
                _log.exception("TTS engine initialisation crashed")
                self._engine, self.problem = None, f"Speech output could not start: {exc}"
            if self._engine is not None:
                log_event(_log, "tts_ready", engine=self._engine.description)
        self._ready.set()
        ready: queue.Queue = queue.Queue(maxsize=1)  # one sentence ready ahead
        player = threading.Thread(target=self._play_loop, args=(ready,), name="tts-player", daemon=True)
        player.start()
        while True:
            item = self._queue.get()
            if item is None:
                ready.put(None)
                player.join(timeout=5)
                return
            clip = None
            try:
                if item.generation == self._generation and self._engine is not None and not self.muted:
                    clip = self._engine.synthesize(item.text, self.speed)
            except (TTSError, AudioError) as exc:
                self.problem = str(exc)
                log_event(_log, "tts_failed", error=str(exc))
            except Exception:
                _log.exception("unexpected TTS failure")
            ready.put((item, clip))

    def _play_loop(self, ready: "queue.Queue") -> None:
        while True:
            entry = ready.get()
            if entry is None:
                return
            item, clip = entry
            try:
                with self._state_lock:  # check-and-clear is atomic w.r.t. stop()
                    stale = item.generation != self._generation
                    if not stale:
                        self._abort.clear()
                if stale or clip is None or self.muted:
                    continue
                self._set_speaking(True)
                self.current_text = item.text
                self._player.play(clip, volume=self.volume, abort=self._abort)
            except (TTSError, AudioError) as exc:
                self.problem = str(exc)
                log_event(_log, "tts_failed", error=str(exc))
            except Exception:
                _log.exception("unexpected TTS failure")
            finally:
                self.current_text = ""
                self._finish(item)
