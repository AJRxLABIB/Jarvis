"""Hands-free listening: always on, waiting for the wake phrase.

One microphone stream stays open. Each 80 ms frame goes to the local
wake-word detector; nothing is recorded or transcribed while waiting. After
"Hey Jarvis" (or the Talk button), the command is collected until a pause and
transcribed locally. After a reply, a short follow-up window accepts another
command without the wake phrase.

Engines:
* ``openwakeword`` (default): a small neural wake-phrase model (see wake_word.py).
* ``whisper``: for wake phrases without a model. Every utterance is transcribed
  locally and dropped unless it starts with the wake phrase (more CPU; the
  dropped text is never shown, logged or stored).

The listener never speaks or runs anything itself: it hands a ``ListenResult``
to the host, which runs it through the normal conversation and tool checks.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np

from app.utils.logging_setup import get_logger, log_event
from app.voice.audio import SAMPLE_RATE, AudioError, rms_dbfs
from app.voice.pipeline import ListenResult, min_confidence
from app.voice.speech_to_text import STTError
from app.voice.vad import EnergyVAD
from app.voice.wake_word import (
    FRAME_SAMPLES, WakeWordDetector, is_bare_name, model_phrase, WakeWordUnavailableError, create_wake_word_detector,
    download_models, is_background_noise, is_filler, missing_models, strip_wake_phrase,
)

if TYPE_CHECKING:
    from app.config.settings import Settings
    from app.voice.microphone import Microphone
    from app.voice.speech_to_text import SpeechToText

_log = get_logger("hands_free")
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
COMMAND_START_TIMEOUT = 5.0  # after the wake phrase, how long to wait for the command to start
GREET_AFTER = 1.0  # "Jarvis" ... (silence): answer "Yes, sir! How can I help you?" after this long
NEAR_MISS_SCORE = 0.15  # wake-model score that is worth double-checking with speech recognition
# Bare "Jarvis" with the "hey jarvis" model: measured with synthesized speech, a bare "Jarvis ..." scored
# 0.015-0.998 and other words at most 0.32. Above the wake threshold it wakes at once; from this score up
# (and every short utterance) speech recognition checks for the name - everything else is ignored unheard.
BARE_NAME_NEAR_MISS = 0.08
NEAR_MISS_MAX_SECONDS = 5.0  # only short utterances ("Jarvis, are you there?") are double-checked
SHORT_UTTERANCE_SECONDS = 3.5  # WAKE_ON_NAME: utterances this short are checked for the name ("Jarvis?")
WHISPER_MAX_UTTERANCE = 8.0  # whisper engine: longer utterances can't be a wake phrase + short command
NO_AUDIO_SECONDS = 5.0
RETRY_MAX_SECONDS = 30.0

State = Literal["starting", "waiting", "command", "follow_up", "busy", "stopped", "error"]


# ------------------------------------------------------------------ utterances


@dataclass
class Utterance:
    audio: np.ndarray
    speech: bool


class UtteranceCollector:
    """Collects one utterance from 80 ms frames: waits for speech, ends after a pause.

    Bursts with less than ``min_speech`` seconds of speech (a cough, a click, the
    tail of the wake phrase) are dropped and waiting continues.
    """

    PREROLL = 3  # frames kept from before speech started (~240 ms)

    def __init__(self, vad, *, start_timeout: float, silence_timeout: float, max_seconds: float,
                 preroll: bool = True, min_speech: float = 0.15) -> None:
        self.vad = vad
        self.start_timeout = start_timeout
        self.silence_timeout = silence_timeout
        self.max_seconds = max_seconds
        self.min_speech = min_speech
        self._preroll: deque[np.ndarray] = deque(maxlen=self.PREROLL if preroll else 0)
        self.reset()

    def reset(self) -> None:
        self._elapsed = 0.0
        self._restart()

    def _restart(self) -> None:
        """Forget the current burst, but keep counting towards ``start_timeout``."""
        self._frames: list[np.ndarray] = []
        self._preroll.clear()
        self._started = False
        self._speech_frames = 0
        self._last_speech = 0.0

    def audio_so_far(self) -> np.ndarray:
        return np.concatenate(self._frames).astype(np.float32) if self._frames else np.zeros(0, np.float32)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def speech_frames(self) -> int:
        return self._speech_frames

    @property
    def pause_seconds(self) -> float:
        """How long it has been quiet since the last speech (0 before speech started)."""
        return self._elapsed - self._last_speech if self._started else 0.0

    def feed(self, frame: np.ndarray, speech: bool) -> Utterance | None:
        """Add a frame (``speech`` from the shared VAD). Returns the utterance once it is complete."""
        self._elapsed += FRAME_SECONDS
        if speech:
            if not self._started:
                self._started = True
                self._frames.extend(self._preroll)
                self._preroll.clear()
            self._last_speech = self._elapsed
            self._speech_frames += 1
        if self._started:
            self._frames.append(frame)
            ended = self._elapsed - self._last_speech >= self.silence_timeout
            if ended and self._speech_frames * FRAME_SECONDS < self.min_speech:
                self._restart()  # too short to be a command
            elif ended or len(self._frames) * FRAME_SECONDS >= self.max_seconds:
                keep = max(1, len(self._frames) - int(max(self.silence_timeout - 0.25, 0) / FRAME_SECONDS))
                audio = np.concatenate(self._frames[:keep]).astype(np.float32)
                self.reset()
                return Utterance(audio, True)
            if self._started:
                return None
        self._preroll.append(frame)
        if self._elapsed >= self.start_timeout:
            self.reset()
            return Utterance(np.zeros(0, dtype=np.float32), False)
        return None


# ---------------------------------------------------------------------- chime


def wake_chime(sample_rate: int = 22050) -> np.ndarray:
    """A short, soft two-note chime (generated, no audio files)."""
    notes = []
    for freq, seconds in ((880.0, 0.07), (1318.5, 0.09)):
        t = np.arange(int(sample_rate * seconds)) / sample_rate
        envelope = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.01)
        notes.append(0.25 * np.sin(2 * np.pi * freq * t) * envelope)
    return np.concatenate(notes).astype(np.float32)


# ------------------------------------------------------------ what was said

_PRESENCE = frozenset({
    "are you there", "you there", "are you awake", "are you listening", "are you up", "you up", "hello", "hi",
    "hey", "hello there", "hey there", "wake up", "yo", "are you with me", "can you hear me", "you listening",
    "are you online", "are you ready", "ready", "hi there"})
_DISMISS = frozenset({
    "goodbye", "good bye", "bye", "bye bye", "that's all", "that is all", "that will be all", "that'll be all",
    "that's it", "go to sleep", "sleep", "stop listening", "nothing", "never mind", "nevermind", "no thanks",
    "no thank you", "see you", "see you later", "later", "dismiss", "dismissed", "you can go", "go away",
    "we're done", "i'm done", "all done", "that's enough", "enough", "rest", "take a rest", "good night"})
_POLITE = frozenset({"ok", "okay", "thanks", "thank", "you", "please", "alright", "so", "and", "well",
                     "now"})


def _phrase(text: str, names: set[str]) -> str:
    words = re.findall(r"[a-z']+", text.lower())
    return " ".join(w for w in words if w not in names)


def _names(settings: "Settings") -> set[str]:
    return {w for w in (settings.wake_word + " " + settings.assistant_name).lower().split()} - {"hey", "hi", "ok", "okay"}


def is_presence_check(text: str, settings: "Settings") -> bool:
    """ "Are you there?", "Hello", "তুমি আছো?", or nothing left after the wake phrase."""
    from app.agent.language import BENGALI, is_bangla_presence

    if BENGALI.search(text or ""):
        return is_bangla_presence(text)
    return _phrase(text, _names(settings)) in _PRESENCE | {""}


def is_dismissal(text: str, settings: "Settings") -> bool:
    """ "Goodbye", "That's all, thanks", "Go to sleep": end the conversation."""
    from app.agent.language import BENGALI, is_bangla_dismissal

    if BENGALI.search(text or ""):
        return is_bangla_dismissal(text)
    phrase = _phrase(text, _names(settings))
    if phrase in _DISMISS:
        return True
    words = [w for w in phrase.split() if w not in _POLITE]
    return 0 < len(words) <= 4 and " ".join(words) in _DISMISS


def wake_greeting(settings: "Settings", heard: str = "") -> str:
    address = settings.user_address.strip()
    from app.agent.language import bangla_greeting, detect_language

    if heard and detect_language(heard) == "bn":
        return bangla_greeting(address)
    return f"Yes, {address}! How can I help you?" if address else "Yes? How can I help you?"


def dismissal_reply(settings: "Settings", heard: str = "") -> str:
    address = settings.user_address.strip()
    from app.agent.language import bangla_dismissal, detect_language

    if heard and detect_language(heard) == "bn":
        return bangla_dismissal(address)
    name = settings.assistant_name
    return f"Alright{', ' + address if address else ''}. Just say {name} when you need me."


# --------------------------------------------------------------------- engine


def resolve_engine(settings: "Settings") -> str:
    """'openwakeword' when a model exists for the wake phrase (or a custom model is set), else 'whisper'."""
    if settings.wake_word_engine != "auto":
        return settings.wake_word_engine
    if settings.wake_word_model is not None or model_phrase(settings.wake_word) is not None:
        return "openwakeword"  # "jarvis" too: the "hey jarvis" model plus a speech-recognition double-check
    return "whisper"


def prepare_detector(settings: "Settings", model_dir: Path, *, allow_download: bool,
                     notice: Callable[[str], None] | None = None) -> WakeWordDetector:
    """Build the openWakeWord detector, downloading its models (about 4 MB, checksummed) if allowed."""
    if missing_models(model_dir, settings.wake_word, settings.wake_word_model):
        if not allow_download:
            raise WakeWordUnavailableError(
                "The wake-word model is not downloaded and offline mode is on. Run 'python run.py --setup-voice' "
                "once while online.")
        if notice is not None:
            notice("Downloading the wake-word model (about 4 MB, once)...")
        download_models(model_dir, settings.wake_word, settings.wake_word_model)
    return create_wake_word_detector(settings.wake_word, "openwakeword", model_dir=model_dir,
                                     threshold=settings.wake_word_threshold, custom_model=settings.wake_word_model)


# ------------------------------------------------------------------- listener


class HandsFreeListener:
    """Background thread. All callbacks are called from that thread."""

    def __init__(
        self,
        settings: "Settings",
        microphone: "Microphone",
        stt: "SpeechToText",
        *,
        detector_factory: Callable[[], WakeWordDetector] | None,
        on_result: Callable[[ListenResult], None],
        on_state: Callable[[str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
        on_wake: Callable[[], None] | None = None,
        should_pause: Callable[[], bool] = lambda: False,
        on_level: Callable[[float], None] | None = None,
        vad=None,
        can_barge_in: Callable[[], bool] = lambda: False,
        on_barge_in: Callable[[], bool] | None = None,
        speaker=None,
        spoken_text: Callable[[], str] = lambda: "",
        recent_speech: Callable[[], str] | None = None,
        can_duplex: Callable[[], bool] = lambda: False,
        on_partial: Callable[[str], None] | None = None,
        wake_check=None,  # app.voice.wake_templates.WakeTemplates: your recordings confirm each wake-up
    ) -> None:
        self.settings = settings
        self.microphone = microphone
        self.stt = stt
        self.engine = "openwakeword" if detector_factory is not None else "whisper"
        # "jarvis" with the "hey jarvis" model: the model picks the candidates, speech recognition confirms
        self._bare_name = detector_factory is not None and is_bare_name(settings.wake_word) \
            and getattr(settings, "wake_word_model", None) is None
        self._detector_factory = detector_factory
        self._detector: WakeWordDetector | None = None
        self.on_result = on_result
        self.on_state = on_state
        self.on_notice = on_notice
        self.on_wake = on_wake
        self.should_pause = should_pause
        self.on_level = on_level
        self.can_barge_in = can_barge_in  # True while the assistant is only speaking
        self.on_barge_in = on_barge_in  # host stops the speech (and a running request); False = not now
        self.speaker = speaker  # SpeakerVerifier: only the enrolled voice gives commands (None = anyone)
        self.spoken_text = spoken_text  # what the assistant is saying now (to recognise its own echo)
        # everything it said in the last seconds (its voice can come back from the speakers after it stops)
        self.recent_speech = recent_speech or (lambda: self.spoken_text())
        self.can_duplex = can_duplex  # True while speaking and fast recognition allows talking over it
        self.on_partial = on_partial  # live transcript while you speak (GPU only)
        self.wake_check = wake_check
        self._recent: deque[np.ndarray] = deque(maxlen=int(1.6 / FRAME_SECONDS))  # audio for the wake check
        self._answer: tuple[Callable[[str], bool], float] | None = None  # voice answer to a yes/no question
        self._answer_collector: UtteranceCollector | None = None
        self._duplex_collector: UtteranceCollector | None = None
        self._partial_at = 0.0
        self._early = None  # (speech frames, transcript) recognized at the last pause
        self._ignore_until = 0.0  # after a wake-up the personal check rejected
        self.state: State = "stopped"
        self._stop = threading.Event()
        self._trigger = threading.Event()
        self._cancel = threading.Event()
        self._resume: bool | None = None  # follow-up wanted? set by resume()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._vad = vad if vad is not None else EnergyVAD()  # SileroVAD in the app
        self._collector: UtteranceCollector | None = None
        self._clock = 0.0  # seconds of audio listened to (not counting while paused): drives the timeouts
        self._deadline = 0.0  # conversation: end after this much listening without a request
        self._command_started = 0.0
        self._greet = False  # after the wake phrase: greet if nothing follows
        self._near_miss = 0.0  # highest wake score during the current utterance
        self._barging = False  # the detector is listening for a barge-in (while the assistant speaks)

    # ------------------------------------------------------------- control

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hands-free", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def trigger(self) -> None:
        """Listen for a command now, without the wake phrase (the Talk button)."""
        self._trigger.set()

    def cancel(self) -> None:
        """Drop the command being recorded (Esc)."""
        self._cancel.set()

    def ask(self, on_answer: Callable[[str], bool], timeout: float = 25.0) -> None:
        """Listen for a spoken answer (e.g. "yes"/"no" to a confirmation). ``on_answer(text)`` returns True
        when it understood the answer; otherwise listening continues until ``timeout`` seconds."""
        with self._lock:
            self._answer = (on_answer, time.monotonic() + timeout)
            self._answer_collector = None

    def cancel_ask(self) -> None:
        with self._lock:
            self._answer = None

    @property
    def asking(self) -> bool:
        return self._answer is not None

    def resume(self, follow_up: bool) -> None:
        """The host finished handling a result: go back to waiting (optionally with a follow-up window)."""
        with self._lock:
            self._resume = follow_up

    # ------------------------------------------------------------- internals

    def _set_state(self, state: State) -> None:
        if state != self.state:
            self.state = state
            log_event(_log, "hands_free_state", state=state)
            if self.on_state is not None:
                self.on_state(state)

    def _notice(self, text: str) -> None:
        if self.on_notice is not None:
            self.on_notice(text)

    def _start_command(self, follow_up: bool = False, *, keep_deadline: bool = False, greet: bool = False) -> None:
        """Record a command. follow_up = the conversation continues: no wake phrase needed until it times out."""
        s = self.settings
        self._collector = UtteranceCollector(
            self._vad, start_timeout=float("inf") if follow_up else COMMAND_START_TIMEOUT,
            silence_timeout=s.silence_timeout, max_seconds=s.max_record_seconds, preroll=follow_up)
        if follow_up and not keep_deadline:
            self._deadline = self._clock + s.follow_up_seconds
        self._command_started = self._clock
        self._greet = greet and not follow_up
        self._early = None
        self._set_state("follow_up" if follow_up else "command")

    def _wait(self) -> None:
        s = self.settings
        # While waiting, utterances are collected too: the whisper engine transcribes them all; the
        # wake-word engine only double-checks short ones that almost triggered it.
        max_seconds = WHISPER_MAX_UTTERANCE if self.engine == "whisper" or self._bare_name else NEAR_MISS_MAX_SECONDS
        self._collector = UtteranceCollector(self._vad, start_timeout=float("inf"),
                                             silence_timeout=s.silence_timeout, max_seconds=max_seconds)
        self._near_miss = 0.0
        if self._detector is not None:
            self._detector.reset()
        self._set_state("waiting")

    def _emit(self, result: ListenResult) -> None:
        self._set_state("busy")
        with self._lock:
            self._resume = None
        self.on_result(result)  # the host calls resume() when it is done

    def _transcribe(self, audio: np.ndarray):
        try:
            return self.stt.transcribe(audio, self.settings.voice_language)
        except STTError as exc:
            self._notice(str(exc))
            return None

    EARLY_PAUSE = 0.3  # seconds of quiet before the sentence is transcribed "early" (while the pause is confirmed)

    def _early_transcribe(self) -> bool:
        """At the first short pause, transcribe what was said so far. If the pause turns out to be the end of the
        command, that transcript is used and the recognition time is saved; if you keep talking it is dropped.
        Only on a GPU (fast enough) and with EARLY_TRANSCRIPTION on. Returns True if it transcribed."""
        collector = self._collector
        if (collector is None or not collector.started or not getattr(self.settings, "early_transcription", True)
                or getattr(self.stt, "device", "cpu") != "cuda"):
            return False
        if collector.pause_seconds < self.EARLY_PAUSE or collector.pause_seconds >= self.settings.silence_timeout:
            return False
        if self._early is not None and self._early[0] == collector.speech_frames:
            return False  # this pause is already transcribed
        audio = collector.audio_so_far()
        keep = audio.size - int(max(collector.pause_seconds - 0.25, 0) * SAMPLE_RATE)
        audio = audio[:max(keep, 0)]
        if audio.size < SAMPLE_RATE * 0.3:
            return False
        transcript = self._transcribe(audio)
        self._early = (collector.speech_frames, transcript) if transcript is not None else None
        if transcript is not None and transcript.text and self.on_partial is not None:
            try:
                self.on_partial(transcript.text)
            except Exception:
                _log.warning("partial transcript callback failed", exc_info=True)
        return True

    def _handle_command(self, utterance: Utterance, transcript=None) -> None:
        """A command was recorded (after the wake phrase, Talk, or in the follow-up window).
        ``transcript``: already recognized at the pause (see _early_transcribe)."""
        was_follow_up = self.state == "follow_up"
        if not utterance.speech:
            if not was_follow_up:
                self._notice("I didn't hear a command.")
            self._wait()
            return
        if not self._is_owner(utterance.audio):  # someone else (or the TV): not a command
            if was_follow_up:
                self._start_command(True, keep_deadline=True)
            else:
                self._wait()
            return
        self._set_state("busy")
        if transcript is None:
            transcript = self._transcribe(utterance.audio)
        if transcript is None or transcript.is_empty:
            self._wait()
            return
        text, _ = strip_wake_phrase(transcript.text, self.settings.wake_word)
        if is_presence_check(text, self.settings):  # "Jarvis?", "are you there?"
            self._emit(ListenResult("wake_only", message=wake_greeting(self.settings, transcript.text), transcript=transcript))
            return
        if is_filler(text) or (was_follow_up and is_background_noise(text)):
            log_event(_log, "filler_ignored")  # "mm-hmm", a cough, TV noise: not a request
            self._start_command(was_follow_up, keep_deadline=True)
            return
        from app.agent.language import is_echo

        if was_follow_up and is_echo(text, self.recent_speech()):
            log_event(_log, "echo_ignored")  # the end of its own answer, heard back from the speakers
            self._start_command(True, keep_deadline=True)
            return
        if was_follow_up and transcript.confidence < min_confidence(self.settings, transcript):
            log_event(_log, "unclear_follow_up_ignored")  # probably background speech: stay quiet
            self._start_command(True, keep_deadline=True)
            return
        if is_dismissal(text, self.settings):  # "goodbye", "that's all"
            self._emit(ListenResult("dismissed", text=text, message=dismissal_reply(self.settings, transcript.text),
                                    transcript=transcript))
            return
        if transcript.confidence < min_confidence(self.settings, transcript):
            self._emit(ListenResult("low_confidence", text=text, transcript=transcript,
                                    message="দুঃখিত, ঠিক বুঝতে পারিনি। আরেকবার বলবেন?" if transcript.language == "bn"
                                    else "Sorry, I didn't catch that. Please say it again."))
        else:
            self._emit(ListenResult("ok", text=text, transcript=transcript, audio=utterance.audio))

    def _handle_whisper_waiting(self, utterance: Utterance) -> None:
        """Only an utterance that starts with the wake phrase (or the assistant's name) counts.
        Used for every utterance by the whisper engine, and for near-misses of the wake-word model."""
        if not utterance.speech or utterance.audio.size < SAMPLE_RATE * 0.3:
            return
        self._set_state("busy")
        transcript = self._transcribe(utterance.audio)
        if transcript is None or transcript.is_empty:
            self._wait()
            return
        text, had_wake = strip_wake_phrase(transcript.text, self.settings.wake_word)
        if not had_wake:
            log_event(_log, "utterance_ignored")  # no content is logged
            self._wait()
            return
        if not self._is_owner(utterance.audio):
            self._wait()
            return
        self._wake()
        if is_presence_check(text, self.settings) or is_filler(text):
            self._emit(ListenResult("wake_only", message=wake_greeting(self.settings, transcript.text), transcript=transcript))
            return
        if is_dismissal(text, self.settings):
            self._emit(ListenResult("dismissed", text=text, message=dismissal_reply(self.settings, transcript.text),
                                    transcript=transcript))
            return
        self._emit(ListenResult("ok", text=text, transcript=transcript))

    def _wake_confirmed(self) -> bool:
        """The personal wake check (if you taught it your wake phrase): does it sound like your recordings?"""
        if self.wake_check is None or not self._recent:
            return True
        try:
            ok = self.wake_check.verify(np.concatenate(list(self._recent)))
        except Exception:
            _log.warning("wake check failed; accepting the wake-up", exc_info=True)
            return True
        if not ok:
            log_event(_log, "wake_rejected")  # sounded unlike your wake phrase (a TV, a similar word)
        return ok

    def _wake(self) -> None:
        log_event(_log, "wake_word_detected", engine=self.engine,
                  score=round(getattr(self._detector, "last_score", 0.0), 3))
        if self.on_wake is not None:
            self.on_wake()

    def _loop(self, stream) -> None:
        self._wait()
        while not self._stop.is_set():
            frame = stream.read(0.5)
            if frame is None:
                if time.monotonic() - stream.last_audio > NO_AUDIO_SECONDS:
                    raise AudioError("The microphone stopped sending audio.")
                continue
            if self.on_level is not None and self.state in ("command", "follow_up"):
                self.on_level(rms_dbfs(frame))

            speech = self._vad.is_speech(frame)  # once per frame: the neural detector keeps state
            if self._answer is not None:
                if self._answer_frame(frame, speech):
                    continue
            if self.state == "busy" and self._duplex_frame(frame, speech):
                stream.discard()
                continue
            if self.state == "busy":
                with self._lock:
                    follow_up, self._resume = self._resume, None
                if follow_up is None:
                    continue
                stream.discard()
                self._vad.reset()
                if follow_up and self.settings.follow_up_seconds > 0:
                    self._start_command(follow_up=True)
                else:
                    self._wait()
                continue

            if self._cancel.is_set():
                self._cancel.clear()
                self._trigger.clear()
                if self.state in ("command", "follow_up"):
                    self._wait()
                continue
            if self.should_pause():  # the assistant is speaking, thinking or asking for confirmation
                if self._collector is not None:
                    self._collector.reset()
                if self._barge_in(frame):
                    stream.discard()
                    continue
                if self.state != "busy" and self._duplex_frame(frame, speech):
                    stream.discard()
                    continue
                if self._detector is not None and self.state == "waiting" and not self._barging:
                    self._detector.reset()
                continue  # (the listening clock stands still, so timeouts only count while it listens)
            if self._barging:
                self._barging = False
                if self._detector is not None:
                    self._detector.reset()
            if self._trigger.is_set():
                self._trigger.clear()
                self._wake()
                self._start_command()
                continue

            self._clock += FRAME_SECONDS
            self._recent.append(frame)
            if self.state == "waiting" and self._collector is not None:
                fired = self._detector is not None and self._detector.process(frame)
                if fired and self._bare_name:
                    fired = False  # bare "Jarvis": let the sentence finish, then check it for the name (below)
                if fired:
                    if not self._wake_confirmed():
                        self._detector.reset()
                        self._collector.reset()  # and don't let the near-miss check revive it
                        self._near_miss, self._ignore_until = 0.0, self._clock + 2.0
                        continue
                    self._wake()
                    self._start_command(greet=True)
                    continue
                if self._detector is not None and self._clock >= self._ignore_until:
                    self._near_miss = max(self._near_miss, getattr(self._detector, "last_score", 0.0))
                utterance = self._collector.feed(frame, speech)
                if utterance is not None and self._clock < self._ignore_until:
                    self._near_miss = 0.0  # the rest of a wake-up the personal check rejected
                elif utterance is not None:
                    near, self._near_miss = self._near_miss, 0.0
                    short = utterance.audio.size <= SHORT_UTTERANCE_SECONDS * SAMPLE_RATE
                    near_miss = BARE_NAME_NEAR_MISS if self._bare_name else NEAR_MISS_SCORE
                    if (self._detector is None or near >= near_miss
                            or (short and self.settings.wake_on_name)):
                        if self._detector is not None:  # the score only, never what was said
                            log_event(_log, "wake_check", score=round(near, 3), short=short)
                        self._handle_whisper_waiting(utterance)
                        stream.discard()
                elif not self._collector.started:
                    self._near_miss = 0.0
            elif self.state in ("command", "follow_up") and self._collector is not None:
                if self.state == "follow_up" and not self._collector.started and self._clock >= self._deadline:
                    log_event(_log, "conversation_ended")  # quiet for FOLLOW_UP_SECONDS: back to the wake phrase
                    self._wait()
                    continue
                if (self._greet and not self._collector.started
                        and self._clock - self._command_started >= GREET_AFTER):
                    self._greet = False  # "Jarvis" and nothing else: answer, then keep listening
                    self._emit(ListenResult("wake_only", message=wake_greeting(self.settings)))
                    continue
                spoken_frames = self._collector.speech_frames
                utterance = self._collector.feed(frame, speech)
                if utterance is not None:
                    early, self._early = self._early, None
                    ready = early[1] if early is not None and early[0] == spoken_frames and not speech else None
                    if ready is not None:
                        log_event(_log, "early_transcript_used")
                    self._handle_command(utterance, ready)
                    stream.discard()
                elif not self._early_transcribe():
                    self._partial()

    def _is_owner(self, audio: np.ndarray) -> bool:
        if self.speaker is None:
            return True
        try:
            ok = self.speaker.verify(audio)
        except Exception:
            _log.warning("speaker verification failed; accepting the command", exc_info=True)
            return True
        if not ok:
            log_event(_log, "other_speaker_ignored")  # no audio or text is logged
        return ok

    def _answer_frame(self, frame: np.ndarray, speech: bool) -> bool:
        """Answer mode (a yes/no question is open). Returns True while it consumed the frame."""
        with self._lock:
            answer = self._answer
        if answer is None:
            return False
        callback, deadline = answer
        if time.monotonic() > deadline:
            self.cancel_ask()
            return False
        if self.spoken_text():  # wait until the question has been read out
            if self._answer_collector is not None:
                self._answer_collector.reset()
            return True
        if self._answer_collector is None:
            self._answer_collector = UtteranceCollector(self._vad, start_timeout=float("inf"),
                                                        silence_timeout=self.settings.silence_timeout, max_seconds=6.0)
        utterance = self._answer_collector.feed(frame, speech)
        if utterance is None or not utterance.speech:
            return True
        self._answer_collector = None
        if not self._is_owner(utterance.audio):
            return True
        transcript = self._transcribe(utterance.audio)
        text = transcript.text if transcript is not None else ""
        try:
            understood = bool(text) and callback(text)
        except Exception:
            _log.warning("answer callback failed", exc_info=True)
            understood = False
        if understood:
            self.cancel_ask()
        return True

    DUPLEX_MAX_SECONDS = 6.0

    def _duplex_frame(self, frame: np.ndarray, speech: bool) -> bool:
        """While the assistant speaks: you can talk over it. Its own voice (echo) is recognised by what
        it says (and by the voice print, if enrolled) and ignored; "stop" stops it; anything else is taken
        as a new command. Returns True if it interrupted."""
        try:
            allowed = self.on_barge_in is not None and self.can_duplex()
        except Exception:
            allowed = False
        if not allowed:
            self._duplex_collector = None
            return False
        if self._duplex_collector is None:
            self._duplex_collector = UtteranceCollector(self._vad, start_timeout=float("inf"), silence_timeout=0.6,
                                                        max_seconds=self.DUPLEX_MAX_SECONDS)
        utterance = self._duplex_collector.feed(frame, speech)
        if utterance is None or not utterance.speech or utterance.audio.size < SAMPLE_RATE * 0.4:
            return False
        self._duplex_collector = None
        if self.speaker is not None and getattr(self.speaker, "enrolled", False) and not self._is_owner(utterance.audio):
            return False  # its own voice (or someone else)
        transcript = self._transcribe(utterance.audio)
        if transcript is None or transcript.is_empty:
            return False
        from app.agent.language import is_echo, is_stop_request

        text, had_wake = strip_wake_phrase(transcript.text, self.settings.wake_word)
        if is_echo(transcript.text, self.recent_speech()) or is_filler(text) or is_background_noise(text):
            return False
        stop = is_stop_request(text)
        if not stop and transcript.confidence < min_confidence(self.settings, transcript):
            return False
        owner_checked = self.speaker is not None and getattr(self.speaker, "enrolled", False)
        if not stop and not had_wake and not owner_checked:
            # Without your voice print, speech while it talks is most likely its own voice from the speakers
            # (or the TV): only "stop" or "Jarvis, ..." interrupts. Enroll your voice to talk over it freely.
            log_event(_log, "talk_over_ignored")
            return False
        try:
            stopped = self.on_barge_in()
        except Exception:
            stopped = False
        if not stopped:
            return False
        log_event(_log, "interrupted_by_voice", stop=stop)
        with self._lock:
            self._resume = None
        self._vad.reset()
        if stop or not text:
            self._start_command(follow_up=True)
        else:
            self._emit(ListenResult("ok", text=text, transcript=transcript))
        return True

    def _partial(self) -> None:
        """Live transcript of the command being spoken (only when recognition is fast: GPU)."""
        if self.on_partial is None or self._collector is None or not self._collector.started:
            return
        if self._clock - self._partial_at < 1.0 or getattr(self.stt, "device", "cpu") != "cuda":
            return
        self._partial_at = self._clock
        audio = self._collector.audio_so_far()
        if audio.size < SAMPLE_RATE * 0.6:
            return
        transcript = self._transcribe(audio)
        if transcript is not None and transcript.text:
            try:
                self.on_partial(transcript.text)
            except Exception:
                _log.warning("partial transcript callback failed", exc_info=True)

    BARGE_IN_SCORE = 0.7  # while it speaks, the wake phrase must be clearer than usual (its own voice is audible)

    def _barge_in(self, frame: np.ndarray) -> bool:
        """"Hey Jarvis" while the assistant is speaking: stop talking and listen for a new command."""
        if self._detector is None or self.on_barge_in is None:
            return False
        try:
            allowed = self.can_barge_in()
        except Exception:
            allowed = False
        if not allowed:
            self._barging = False
            return False
        self._barging = True
        if not self._detector.process(frame) or getattr(self._detector, "last_score", 1.0) < self.BARGE_IN_SCORE:
            return False
        try:
            stopped = self.on_barge_in()
        except Exception:
            _log.warning("barge-in failed", exc_info=True)
            stopped = False
        if not stopped:
            return False
        log_event(_log, "barge_in", score=round(getattr(self._detector, "last_score", 0.0), 3))
        with self._lock:
            self._resume = None
        self._barging = False
        self._vad.reset()
        self._wake()
        self._start_command()
        return True

    def _retry_delay(self, attempt: int) -> float:
        return min(RETRY_MAX_SECONDS, 2.0 * 2 ** (attempt - 1))  # 2, 4, 8, 16, 30, 30...

    def _prepare(self) -> bool:
        """Load speech recognition and the wake-word model, retrying until they work (or stop)."""
        attempt = 0
        while not self._stop.is_set():
            try:
                if not self.stt.loaded:
                    self.stt.load()
                if self._detector_factory is not None and self._detector is None:
                    self._detector = self._detector_factory()
                return True
            except (STTError, WakeWordUnavailableError) as exc:
                attempt += 1
                log_event(_log, "hands_free_prepare_failed", error=str(exc), attempt=attempt)
                if attempt == 1:
                    self._notice(f"Hands-free listening is waiting: {exc} I'll keep trying.")
                    self._set_state("error")
                self._stop.wait(self._retry_delay(attempt))
        return False

    def _run(self) -> None:
        """Runs until stop(): problems (no microphone yet at sign-in, a USB headset unplugged, the
        wake-word model still downloading) are retried with a growing pause, never given up on."""
        self._set_state("starting")
        if not self._prepare():
            self._set_state("stopped")
            return
        failures = 0
        told = False
        while not self._stop.is_set():
            try:
                with self.microphone.open_stream(FRAME_SAMPLES) as stream:
                    if told:
                        self._notice("Hands-free listening is working again.")
                    failures, told = 0, False
                    self._loop(stream)
            except AudioError as exc:
                failures += 1
                log_event(_log, "hands_free_audio_error", error=str(exc), attempt=failures)
                if failures == 3 and not told:  # the first two retries are quick and silent
                    self._notice(f"Hands-free listening is waiting for the microphone ({exc}). I'll keep trying.")
                    self._set_state("error")
                    told = True
                self._stop.wait(self._retry_delay(failures))
            except Exception:
                failures += 1
                _log.exception("hands-free listener crashed; restarting it")
                if not told:
                    self._notice("Hands-free listening hit an error (details in the log). Restarting it...")
                    self._set_state("error")
                    told = True
                self._stop.wait(self._retry_delay(failures))
        self._set_state("stopped")
