"""Local speech recognition (faster-whisper).

The model is loaded lazily on first use (or explicitly with ``load()``), and
downloaded into ``data/models/whisper`` the first time. After that it works
offline. In offline mode it never tries to download.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from app.utils.logging_setup import get_logger, log_event
from app.voice.audio import SAMPLE_RATE, remove_rumble

if TYPE_CHECKING:
    from app.voice.stt_select import ModelChoice

_log = get_logger("stt")

# Phrases Whisper tends to invent from silence or noise.
_HALLUCINATIONS = frozenset(
    {
        "thank you", "thanks for watching", "thank you for watching", "thanks for watching!",
        "you", "bye", "subtitles by the amara.org community", "please subscribe", ".", "", "thanks", "thank you so much",
        "all right", "alright", "okay", "ok", "hmm", "so", "oh", "yeah", "thanks for listening", "bye bye",
    }
)


class STTError(Exception):
    """Speech recognition problem with a user-readable message."""


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    confidence: float  # 0..1, derived from the decoder's average log-probability
    no_speech_prob: float
    audio_seconds: float
    elapsed_ms: int

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class SpeechToText(ABC):
    name = "base"

    @property
    @abstractmethod
    def loaded(self) -> bool: ...

    @abstractmethod
    def load(self) -> None:
        """Load (and if needed download) the model. Raises STTError."""

    @abstractmethod
    def transcribe(self, audio: np.ndarray, language: str | None = None) -> Transcript:
        """Transcribe float32 mono audio at 16 kHz. Raises STTError."""


def _normalized(text: str) -> str:
    return re.sub(r"[^\w\s!]", "", text.lower()).strip()


# What a quiet or clipped command often comes out as.
FILLERS = frozenset({"thank you", "thanks", "thank you so much", "all right", "alright", "okay", "ok", "you", "bye",
                     "bye bye", "so", "oh", "yeah", "hmm", "right"})

# Things Whisper writes from noise, keyboard clicks or music, whatever the confidence says.
_NOISE_TEXT = re.compile(r"msworddoc|word\.document|microsoft word document|amara\.org|subtitles by|transcribed by|"
                         r"transcription by|captions by|www\.|\.com\b|thank you for watching|please subscribe|"
                         r"like and subscribe|see you (?:in the )?next (?:time|video)|\[(?:music|applause|noise)\]", re.I)


def is_hallucination(text: str, no_speech_prob: float, audio_seconds: float | None = None) -> bool:
    if not text.strip():
        return True
    if _normalized(text) in _HALLUCINATIONS and no_speech_prob > 0.3:
        return True
    if _NOISE_TEXT.search(text):
        return True
    words = re.findall(r"\w+", text.lower())
    if len(words) >= 4 and len(set(words)) <= len(words) // 3:  # "word word word word ..."
        return True
    # nobody says 39 characters in 0.4 seconds: it was made up from a click or a bump
    return audio_seconds is not None and audio_seconds < 1.5 and len(text.strip()) > 26 * audio_seconds + 12


def _friendly_load_error(exc: BaseException, model: str, device: str, offline: bool) -> STTError:
    detail = str(exc).lower()
    if any(word in detail for word in ("cublas", "cudnn", "cuda", "libcu")):
        return STTError(
            "Speech recognition could not use the GPU (CUDA 12 / cuDNN 9 libraries not found). "
            "Set STT_DEVICE=cpu in .env, or install the NVIDIA libraries."
        )
    if offline or any(word in detail for word in ("local_files_only", "connection", "resolve", "offline", "snapshot")):
        return STTError(
            f"The speech model '{model}' is not downloaded yet. Connect to the internet and run "
            "'python run.py --setup-voice'."
        )
    return STTError(f"The speech model '{model}' could not be loaded on {device}: {exc}")


def _default_downloaded(model: str, root: Path) -> bool:
    if Path(model).is_absolute():
        return Path(model).is_dir()
    try:
        from faster_whisper.utils import download_model

        download_model(model, local_files_only=True, cache_dir=str(root))
        return True
    except Exception:
        return False


def _download(model: str, root: Path) -> None:
    from faster_whisper.utils import download_model

    download_model(model, cache_dir=str(root))


def _score(segments) -> tuple[str, float, float]:
    """(text, confidence 0..1 from the average log-probability, no-speech probability)."""
    text = " ".join(seg.text.strip() for seg in segments).strip()
    if not segments:
        return text, 0.0, 1.0
    weights = [max(len(seg.text), 1) for seg in segments]
    logprob = sum(seg.avg_logprob * w for seg, w in zip(segments, weights)) / sum(weights)
    return text, max(0.0, min(1.0, math.exp(logprob))), min(seg.no_speech_prob for seg in segments)


def choose_language(info: Any, allowed: tuple[str, ...]) -> str | None:
    """The most likely of the allowed languages, from the model's language probabilities."""
    detected = getattr(info, "language", None)
    if detected in allowed:
        return detected
    probabilities = getattr(info, "all_language_probs", None) or []
    best = max(((p, code) for code, p in probabilities if code in allowed), default=None)
    return best[1] if best else allowed[0]


SILENCE_PEAK = 10 ** (-50 / 20)  # quieter than -50 dBFS everywhere = no speech
MAX_GAIN = 12.0  # quiet microphones: amplify at most this much before recognition
TARGET_PEAK = 0.6


def normalize_level(audio: np.ndarray, target: float = TARGET_PEAK, max_gain: float = MAX_GAIN) -> np.ndarray:
    """Bring quiet speech up to a normal level (Whisper is noticeably worse on very quiet input)."""
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1e-4 and audio.size > 1600:
        # a single click or pop must not decide the gain: use the 99.5th percentile of the loudness as the "peak"
        peak = max(float(np.percentile(np.abs(audio), 99.5)) * 1.5, 1e-4) if peak > 3 * float(np.percentile(np.abs(audio), 99.5)) else peak
    if peak <= 1e-4 or peak >= target:
        return audio
    gain = min(max_gain, target / peak)
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)


class FasterWhisperSTT(SpeechToText):
    """faster-whisper with fallbacks.

    ``candidates`` are tried best first (see stt_select): a model that is already
    downloaded is used right away; if a better one isn't downloaded yet it is fetched
    in the background and swapped in when ready. A GPU that fails (missing CUDA
    libraries) falls back to the CPU, also when that only shows during recognition.
    """

    name = "faster-whisper"

    def __init__(
        self,
        model: str,
        *,
        download_root: Path,
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 5,
        offline: bool = False,
        cpu_threads: int = 0,
        hotwords: str | None = None,
        model_factory: Callable[..., Any] | None = None,
        candidates: "list[ModelChoice] | None" = None,
        downloaded: Callable[[str, Path], bool] | None = None,
        downloader: Callable[[str, Path], None] | None = None,
        background_upgrade: bool = True,
        allowed_languages: tuple[str, ...] | None = None,
        second_pass: bool = True,
        bangla_model: str = "",
    ) -> None:
        from app.voice.stt_select import ModelChoice

        self.candidates: list[ModelChoice] = list(candidates or [ModelChoice(model, device, compute_type)])
        first = self.candidates[0]
        self.model_name, self.device, self.compute_type = first.model, first.device, first.compute_type
        self.download_root = Path(download_root)
        self.beam_size = beam_size
        self.offline = offline
        self.cpu_threads = cpu_threads or max(1, min(8, (os.cpu_count() or 4) - 1))
        # Words the recognizer should expect (e.g. the assistant's name). Small
        # models otherwise hear "Hey Jarvis" as "Hey, garbage".
        self.hotwords = hotwords or None
        self.hotwords_bn: str | None = None   # Bangla words you taught it (app/voice/speech_dataset.bangla_hints)
        self._model_factory = model_factory
        self._downloaded = downloaded or ((lambda m, r: True) if model_factory is not None else _default_downloaded)
        self._downloader = downloader or _download
        self.background_upgrade = background_upgrade
        # language "auto": detect, but only among these (e.g. en + bn: never Hindi for accented Bangla)
        self.allowed_languages = tuple(allowed_languages or ())
        self.second_pass = second_pass
        # STT_BANGLA_MODEL: a Whisper model fine-tuned for Bangla (CTranslate2 format: a folder or a Hugging Face
        # repo). When the sentence is Bangla, it is recognised again with that model, which knows Bangla better.
        self.bangla_model = (bangla_model or "").strip()
        self._bangla: Any = None
        self._bangla_failed = False
        self._model: Any = None
        self._lock = threading.Lock()
        self._upgrading: threading.Thread | None = None
        self.on_model_change: Callable[[str], None] | None = None  # e.g. the UI shows "large-v3-turbo on GPU"

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def description(self) -> str:
        where = "GPU" if self.device == "cuda" else "CPU"
        return f"{self.model_name} on {where}"

    def _factory(self) -> Callable[..., Any]:
        if self._model_factory is not None:
            return self._model_factory
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTError("Speech recognition is not installed. Run 'pip install -r requirements.txt'.") from exc
        return WhisperModel

    def _bangla_decode(self, audio: np.ndarray) -> tuple[str, float, float] | None:
        """Recognise a Bangla sentence again with the Bangla specialist model (loaded on first use)."""
        if self._bangla_failed:
            return None
        from app.voice.stt_select import ModelChoice

        try:
            if self._bangla is None:
                self._bangla = self._create(ModelChoice(self.bangla_model, self.device, self.compute_type), self.offline)
                log_event(_log, "stt_bangla_model_loaded", device=self.device)
            segments, _ = self._bangla.transcribe(audio, language="bn", beam_size=self.beam_size, vad_filter=True,
                                                  condition_on_previous_text=False, without_timestamps=True)
            return _score(list(segments))
        except Exception:
            self._bangla_failed = True  # don't retry every sentence; the normal model keeps working
            _log.warning("the Bangla speech model (STT_BANGLA_MODEL) could not be used", exc_info=True)
            return None

    def _create(self, choice, local_only: bool) -> Any:
        if choice.device == "cuda":
            from app.voice.stt_select import register_cuda_libraries

            register_cuda_libraries()
        return self._factory()(
            choice.model,
            device=choice.device,
            compute_type=choice.compute_type,
            cpu_threads=self.cpu_threads,
            download_root=str(self.download_root),
            local_files_only=local_only,
        )

    def _use(self, model: Any, choice, started: float) -> None:
        self._model = model
        self.model_name, self.device, self.compute_type = choice.model, choice.device, choice.compute_type
        log_event(
            _log, "stt_model_loaded", model=choice.model, device=choice.device,
            compute_type=choice.compute_type, ms=int((time.perf_counter() - started) * 1000),
        )
        if self.on_model_change is not None:
            try:
                self.on_model_change(self.description)
            except Exception:
                _log.warning("model change callback failed", exc_info=True)

    def load(self) -> None:
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        if self._model is not None:
            return
        # Model downloads are anonymous: no telemetry, no "set HF_TOKEN" nagging,
        # no symlink warning on Windows without developer mode.
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_VERBOSITY", "error")
        warnings.filterwarnings("ignore", message=".*unauthenticated requests.*")
        self.download_root.mkdir(parents=True, exist_ok=True)
        errors: list[tuple[Any, BaseException]] = []
        if len(self.candidates) == 1:
            choice = self.candidates[0]
            started = time.perf_counter()
            try:
                self._use(self._create(choice, self.offline), choice, started)
                return
            except STTError:
                raise
            except Exception as exc:
                _log.warning("speech model load failed", exc_info=True)
                raise _friendly_load_error(exc, choice.model, choice.device, self.offline) from exc
        # 1) the best candidate that is already downloaded (no waiting for a download)
        missing: list[Any] = []
        for choice in self.candidates:
            if not self._downloaded(choice.model, self.download_root):
                missing.append(choice)
                continue
            started = time.perf_counter()
            try:
                self._use(self._create(choice, True), choice, started)
            except STTError:
                raise
            except Exception as exc:
                _log.warning("speech model %s on %s failed to load", choice.model, choice.device, exc_info=True)
                errors.append((choice, exc))
                continue
            better = [c for c in missing if self.candidates.index(c) < self.candidates.index(choice)]
            if better and not self.offline and self.background_upgrade:
                self._start_upgrade(better[0])
            return
        # 2) nothing usable downloaded: download, best first
        if not self.offline:
            for choice in self.candidates:
                if any(c.model == choice.model and c.device == choice.device for c, _ in errors):
                    continue
                started = time.perf_counter()
                try:
                    self._use(self._create(choice, False), choice, started)
                    return
                except STTError:
                    raise
                except Exception as exc:
                    _log.warning("speech model %s download/load failed", choice.model, exc_info=True)
                    errors.append((choice, exc))
        choice, exc = errors[0] if errors else (self.candidates[0], OSError("snapshot not downloaded"))
        raise _friendly_load_error(exc, choice.model, choice.device, self.offline or not errors) from exc

    def _start_upgrade(self, choice) -> None:
        if self._upgrading is not None and self._upgrading.is_alive():
            return

        def work() -> None:
            try:
                log_event(_log, "stt_model_downloading", model=choice.model)
                self._downloader(choice.model, self.download_root)
                started = time.perf_counter()
                model = self._create(choice, True)
            except Exception:
                _log.warning("background download of %s failed; keeping %s", choice.model, self.model_name,
                             exc_info=True)
                return
            with self._lock:
                if choice in self.candidates:  # not if the GPU failed meanwhile
                    self._use(model, choice, started)

        self._upgrading = threading.Thread(target=work, name="stt-upgrade", daemon=True)
        self._upgrading.start()

    def _fall_back_from_gpu(self) -> bool:
        """The GPU failed while recognizing: drop the GPU setups and load the next one. Lock held."""
        remaining = [c for c in self.candidates if c.device != "cuda"]
        if not remaining or remaining == self.candidates:
            return False
        log_event(_log, "stt_gpu_failed_using_cpu", model=remaining[0].model)
        self.candidates = remaining
        self._model = None
        self._load_locked()
        return True

    FILLER_RETRY_SECONDS = 4.0  # a short clip that came out as "Thank you." is decoded once more
    FILLER_KEEP_ABOVE = 0.6  # ... and kept only if the recognizer was sure (you really said "thanks")
    LANGUAGE_CHECK_BELOW = 0.6  # unsure of an English sentence ...
    OTHER_LANGUAGE_ABOVE = 0.15  # ... and Whisper gave Bangla at least this chance: decode it as Bangla too
    LANGUAGE_MARGIN = 0.05  # the other language must be clearly surer to win

    def _other_language(self, info: Any, forced: str | None, confidence: float) -> str | None:
        """The other language of yours worth a second look, or None."""
        if forced is not None or self.device != "cuda" or len(self.allowed_languages or ()) < 2:
            return None
        if confidence >= self.LANGUAGE_CHECK_BELOW:
            return None
        detected = getattr(info, "language", None)
        probabilities = dict(getattr(info, "all_language_probs", None) or [])
        others = [(probabilities.get(code, 0.0), code) for code in self.allowed_languages if code != detected]
        best = max(others, default=None)
        return best[1] if best and best[0] >= self.OTHER_LANGUAGE_ABOVE else None

    SECOND_PASS_BELOW = 0.55  # confidence under this: decode again, more carefully (only on the GPU: fast)
    SECOND_PASS_BEAM = 10

    def _decode(self, audio: np.ndarray, language: str | None, careful: bool = False, vad: bool = True, hotwords: str | None = None):
        extra = {"patience": 2.0, "best_of": 5} if careful else {}
        return self._model.transcribe(
            audio,
            language=language,
            beam_size=self.SECOND_PASS_BEAM if careful else self.beam_size,
            **extra,
            vad_filter=vad,
            condition_on_previous_text=False,
            without_timestamps=True,
            # the English hint words would pull Bangla speech towards English: Bangla gets its own (your corrections)
            hotwords=hotwords or (self.hotwords if language in (None, "en") else (self.hotwords_bn if language == "bn" else None)),
        )

    ANSWER_HOTWORDS = "yes no yeah okay confirm confirmed cancel stop"

    def transcribe(self, audio: np.ndarray, language: str | None = None, *, answer: bool = False) -> Transcript:
        """``answer=True``: a short reply to a yes / no question. "Yes", "yeah", "okay" are real answers here, so the filler and short-text filters
        (which exist to drop "Thank you." made up from noise) are skipped, and the recognizer is nudged towards yes / no / confirm / cancel."""
        self.load()
        audio = np.asarray(audio, dtype=np.float32)
        seconds = audio.size / SAMPLE_RATE
        started = time.perf_counter()
        if seconds < 0.2:
            return Transcript("", language, 0.0, 1.0, seconds, 0)
        if float(np.max(np.abs(audio))) < SILENCE_PEAK:  # nothing but silence: Whisper would invent "Thank you."
            return Transcript("", language, 0.0, 1.0, seconds, 0)
        raw = remove_rumble(audio)
        audio = normalize_level(raw)
        lang = None if language in (None, "", "auto") else language
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._model is None:
                        self._load_locked()
                    segments, info = self._decode(audio, lang, hotwords=self.ANSWER_HOTWORDS if answer else None)
                    if lang is None and len(self.allowed_languages) > 1:
                        chosen = choose_language(info, self.allowed_languages)
                        if chosen is not None and chosen != getattr(info, "language", None):
                            segments, info = self._decode(audio, chosen)  # detected something else: redo
                    segments = list(segments)  # decoding happens while iterating
                    break
                except STTError:
                    raise
                except Exception as exc:
                    from app.voice.stt_select import is_cuda_error

                    if attempt == 1 and self.device == "cuda" and is_cuda_error(exc) and self._fall_back_from_gpu():
                        continue
                    _log.warning("transcription failed", exc_info=True)
                    raise STTError("Speech recognition failed. The details are in the log.") from exc

            text, confidence, no_speech = _score(segments)
            other = self._other_language(info, lang, confidence) if segments else None
            if other is not None:
                try:  # "গুগল ক্রোম খোলো" heard as English "Google Chrome Club": try the other language too
                    again, info2 = self._decode(audio, other)
                    again = list(again)
                    text2, confidence2, no_speech2 = _score(again)
                    switched = bool(text2) and confidence2 > confidence + self.LANGUAGE_MARGIN
                    log_event(_log, "stt_language_second_look", language=other, before=round(confidence, 3),
                              after=round(confidence2, 3), switched=switched)
                    if switched:
                        segments, info, text, confidence, no_speech = again, info2, text2, confidence2, no_speech2
                except Exception:
                    _log.warning("second-language recognition failed", exc_info=True)
            if (self.second_pass and self.device == "cuda" and segments and confidence < self.SECOND_PASS_BELOW):
                try:  # unsure: a slower, wider search often gets Bangla / names right
                    again, info2 = self._decode(audio, getattr(info, "language", None) or lang, careful=True)
                    again = list(again)
                    text2, confidence2, no_speech2 = _score(again)
                    log_event(_log, "stt_second_pass", before=round(confidence, 3), after=round(confidence2, 3))
                    if confidence2 > confidence:
                        segments, info, text, confidence, no_speech = again, info2, text2, confidence2, no_speech2
                except Exception:
                    _log.warning("second recognition pass failed", exc_info=True)

            if self.bangla_model and (getattr(info, "language", None) or lang) == "bn" and segments:
                special = self._bangla_decode(audio)
                if special is not None:
                    text2, confidence2, no_speech2 = special
                    if text2:
                        log_event(_log, "stt_bangla_model_used", before=round(confidence, 3), after=round(confidence2, 3))
                        text, confidence, no_speech = text2, max(confidence, confidence2), no_speech2

            if segments and not answer and _normalized(text) in FILLERS and seconds < self.FILLER_RETRY_SECONDS:
                # "Volume up" heard as "Thank you.": a quiet or clipped command. Once more, louder and without
                # the voice filter (it can cut short words); still a filler and unsure -> not heard at all.
                try:
                    loud = normalize_level(raw, target=0.9, max_gain=40.0)
                    again, info3 = self._decode(loud, getattr(info, "language", None) or lang, careful=True,
                                                vad=False)
                    again = list(again)
                    text3, confidence3, no_speech3 = _score(again)
                    switched = bool(text3) and _normalized(text3) not in FILLERS and not _NOISE_TEXT.search(text3)
                    log_event(_log, "stt_filler_retry", switched=switched, before=round(confidence, 3),
                              after=round(confidence3, 3))
                    if switched:
                        segments, info, text, confidence, no_speech = again, info3, text3, confidence3, no_speech3
                    elif confidence < self.FILLER_KEEP_ABOVE:
                        log_event(_log, "stt_filler_dropped")
                        text = ""
                except Exception:
                    _log.warning("filler retry failed", exc_info=True)

        elapsed = int((time.perf_counter() - started) * 1000)
        if (_NOISE_TEXT.search(text) if answer else is_hallucination(text, no_speech, audio.size / SAMPLE_RATE if hasattr(audio, "size") else None)):
            if text:
                log_event(_log, "stt_noise_dropped", characters=len(text))
            text = ""
        detected = getattr(info, "language", None) or lang
        log_event(
            _log, "stt_transcribed", audio_seconds=round(seconds, 2), ms=elapsed, model=self.model_name,
            device=self.device, confidence=round(confidence, 3), language=detected, characters=len(text),
        )
        return Transcript(text, detected, confidence, no_speech, seconds, elapsed)
