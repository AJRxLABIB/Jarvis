"""Speech output manager: engine selection, background speech queue, stop/mute.

``speak()`` never blocks the caller. A single worker thread synthesizes and
plays queued sentences in order. ``stop()`` interrupts the current sentence
and discards everything queued.
"""

from __future__ import annotations

import inspect
import math
import queue
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

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
        from app.tts import jarvis_voice as jv
        from app.tts.neural import NeuralTTS

        style = getattr(settings, "tts_style", "natural")
        rate, pitch = ((jv.CLASSIC_RATE_OFFSET, jv.CLASSIC_PITCH_OFFSET) if style == "classic"
                       else (jv.RATE_OFFSET, jv.PITCH_OFFSET))
        offline, problem = create_engine(settings.model_copy(update={"tts_engine": "auto",
                                                                     "tts_kokoro_voice": jv.OFFLINE_VOICE}), voice_dir)
        neural = NeuralTTS(jv.ENGLISH_VOICE, settings.tts_neural_voice_bn, fallback=offline,
                           rate_offset=rate, pitch_offset=pitch, gentle=style != "classic")
        try:
            neural.load()
            return jv.JarvisVoice(neural, style=style), None
        except TTSError as exc:
            log_event(_log, "tts_engine_unavailable", engine="jarvis-neural", reason=str(exc))
            if offline is not None:
                return jv.JarvisVoice(offline, style=style), None
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
    clip: "AudioClip | None" = None      # already made (resuming the rest of an interrupted sentence)


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
        # what went to the speakers and when: (monotonic time, rms) - the echo gate compares the microphone with it
        self._levels: deque[tuple[float, float]] = deque(maxlen=200)
        # talk-over: while you speak, the rest of the answer is held here and either resumed or dropped
        self.holding = False
        self._held: list[str] = []
        self._unplayed: list[_Item] = []
        self._closing = threading.Event()
        self._playing: tuple[str, AudioClip] | None = None
        self._played_samples = 0
        self._held_tail: tuple[str, AudioClip] | None = None
        # short phrases Jarvis says again and again ("Yes, sir! How can I help you?", "Done.") are kept ready-made,
        # so they start instantly instead of waiting for the voice service
        self._cache: "OrderedDict[tuple[str, float], AudioClip]" = OrderedDict()
        self._player_reports_levels = "on_chunk" in inspect.signature(self._player.play).parameters

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
        self._closing.set()
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
        with self._state_lock:
            if self.holding:                     # you're talking: keep it for later instead of speaking over you
                self._held.append(text)
                done.set()
                return done
        self.start()
        with self._state_lock:
            self._pending += 1
            self._idle.clear()
            item = _Item(text, self._generation, done)
            self._unplayed.append(item)
        self._queue.put(item)
        return done

    def stop(self) -> None:
        """Stop the current sentence and drop everything queued."""
        with self._state_lock:
            self._generation += 1
            self._abort.set()

    # ------------------------------------------------------------- talk-over

    def playback_db(self, window: float = 0.4) -> float:
        """How loud Jarvis has been in the last ``window`` seconds (dBFS; -100 = silent). Covers the speaker delay."""
        now = time.monotonic()
        recent = [rms for at, rms in list(self._levels) if now - at <= window]
        peak = max(recent) if recent else 0.0
        return 20 * math.log10(peak) if peak > 1e-5 else -100.0

    def speech_level(self) -> float | None:
        """Loudness of the voice playing right now, 0..1 (for the core's speaking animation). None when the player
        doesn't report levels or nothing was reported in the last 0.25 s (the core then uses its own cadence)."""
        if not self._player_reports_levels:
            return None
        now = time.monotonic()
        recent = [rms for at, rms in list(self._levels) if now - at <= 0.25]
        if not recent:
            return None
        db = 20 * math.log10(recent[-1]) if recent[-1] > 1e-5 else -100.0
        return max(0.0, min(1.0, (db + 48.0) / 40.0))

    def hold(self) -> bool:
        """You started talking: stop the sound at once, keep the unspoken rest (current sentence included)."""
        with self._state_lock:
            if self.holding:
                return True
            if self._idle.is_set():
                return False
            # every sentence not yet heard to the end: the one playing, the one synthesized ahead, the queued ones
            rest = [item.text for item in self._unplayed if item.generation == self._generation]
            # the sentence that was playing: keep exactly what wasn't heard yet, so resuming doesn't repeat it
            self._held_tail = None
            playing = self._playing
            if playing is not None and rest and rest[0] == playing[0]:
                text, clip, done = playing[0], playing[1], self._played_samples
                back = int(clip.sample_rate * 0.25)                     # a quarter second back: words aren't clipped
                start = max(0, done - back)
                if clip.samples.size - start > clip.sample_rate * 0.3:
                    tail = clip.samples[start:].copy()
                    fade = min(int(clip.sample_rate * 0.03), tail.size)
                    tail[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
                    self._held_tail = (text, AudioClip(tail, clip.sample_rate))
                rest = rest[1:] if self._held_tail is not None or clip.samples.size - start <= clip.sample_rate * 0.3 \
                    else rest
            self._held = rest
            self.holding = True
            self._generation += 1
            self._abort.set()
        self._set_speaking(False)
        return True

    def resume_held(self) -> None:
        """It wasn't you after all (a cough, the TV): carry on from the interrupted sentence."""
        with self._state_lock:
            held, self._held, self.holding = self._held, [], False
            tail, self._held_tail = self._held_tail, None
            idle_now = self._pending <= 0
        if tail is not None:                       # the rest of the interrupted sentence, from where it stopped
            self._queue_clip(*tail)
        for text in held:
            self.speak(text)
        if not held and tail is None and idle_now:
            self._become_idle()

    def _queue_clip(self, text: str, clip: "AudioClip") -> None:
        done = threading.Event()
        self.start()
        with self._state_lock:
            self._pending += 1
            self._idle.clear()
            item = _Item(text, self._generation, done, clip)
            self._unplayed.append(item)
        self._queue.put(item)

    def discard_held(self) -> None:
        """You said something: the rest of the old answer is dropped."""
        with self._state_lock:
            self._held, self.holding, self._held_tail = [], False, None
            idle_now = self._pending <= 0
        if idle_now:
            self._become_idle()

    @property
    def held_text(self) -> str:
        return " ".join(self._held)

    def _become_idle(self) -> None:
        with self._state_lock:
            if self._pending <= 0 and not self.holding:
                self._pending = 0
                self._idle.set()
        if self._idle.is_set():
            self._set_speaking(False)

    CACHE_MAX_CHARS = 80
    CACHE_SIZE = 64

    def _cached(self, text: str) -> "AudioClip | None":
        key = (text, round(self.speed, 2))
        clip = self._cache.get(key)
        if clip is not None:
            self._cache.move_to_end(key)
        return clip

    def _synthesize(self, text: str) -> "AudioClip":
        clip = self._engine.synthesize(text, self.speed)
        if len(text) <= self.CACHE_MAX_CHARS and clip is not None and clip.samples.size and self._engine_online():
            self._cache[(text, round(self.speed, 2))] = clip
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)
        return clip

    def _engine_online(self) -> bool:
        """Don't keep the offline stand-in voice for later when the online voice is just briefly unavailable."""
        inner = getattr(self._engine, "inner", self._engine)
        until = getattr(inner, "_offline_until", 0.0)
        return time.monotonic() >= until

    def prewarm(self, phrases: list[str], delay: float = 0.0) -> None:
        """Make the usual short replies ahead of time, in the background, when the voice is idle."""
        self.start()

        def run() -> None:
            if delay and self._closing.wait(delay):
                return                               # closed before it was time
            if not self._ready.wait(60) or self._engine is None:
                return
            for phrase in phrases:
                text = clean_for_speech(phrase)
                if not text or self._cached(text) is not None:
                    continue
                if not self._idle.wait(30):
                    return                           # busy talking for a long time: try another time
                try:
                    self._synthesize(text)
                except Exception:
                    return
        threading.Thread(target=run, name="tts-prewarm", daemon=True).start()

    def _note_level(self, rms: float) -> None:
        self._levels.append((time.monotonic(), rms))
        playing = self._playing
        if playing is not None:
            self._played_samples += int(playing[1].sample_rate * 0.05)      # the player reports 50 ms pieces

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
            if item in self._unplayed:
                self._unplayed.remove(item)
            self._pending -= 1
            if self._pending <= 0 and not self.holding:   # while held, it still counts as speaking (nothing resumes)
                self._pending = 0
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
                    clip = item.clip or self._cached(item.text) or self._synthesize(item.text)
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
                self._playing, self._played_samples = (item.text, clip), 0
                if self._player_reports_levels:
                    self._player.play(clip, volume=self.volume, abort=self._abort, on_chunk=self._note_level)
                else:                                    # a player without level reports (tests, plug-ins)
                    self._player.play(clip, volume=self.volume, abort=self._abort)
            except (TTSError, AudioError) as exc:
                self.problem = str(exc)
                log_event(_log, "tts_failed", error=str(exc))
            except Exception:
                _log.exception("unexpected TTS failure")
            finally:
                self.current_text = ""
                self._playing = None
                self._finish(item)
