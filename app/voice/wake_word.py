"""Wake-word support.

``WakeWordDetector`` is the interface for always-listening local detectors
(80 ms frame in, "detected?" out).

``OnnxWakeWordDetector`` runs openWakeWord's models (Apache-2.0,
github.com/dscripka/openWakeWord) directly with onnxruntime, which
faster-whisper already installs: a mel-spectrogram model, a speech-embedding
model and a small wake-phrase model ("hey jarvis" is pre-trained; a custom
openWakeWord .onnx can be used for other phrases). Everything runs locally; the
audio never leaves the computer and is never stored.

``strip_wake_phrase`` removes a spoken wake phrase from a transcript, so
"Hey Jarvis, open VS Code" becomes "open VS Code" in every mode.
"""

from __future__ import annotations

import hashlib
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("wake_word")


class WakeWordUnavailableError(Exception):
    pass


class WakeWordDetector(ABC):
    name = "base"
    frame_samples = 1280  # 80 ms at 16 kHz

    @abstractmethod
    def process(self, frame: np.ndarray) -> bool:
        """Feed one float32 16 kHz frame; True when the wake word was heard."""

    def reset(self) -> None:
        """Clear internal state after a detection."""


# ------------------------------------------------------------ openWakeWord

_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"


@dataclass(frozen=True)
class ModelFile:
    name: str
    sha256: str  # pinned: a changed or tampered download is refused

    @property
    def url(self) -> str:
        return _RELEASE + self.name


FEATURE_MODELS = (
    ModelFile("melspectrogram.onnx", "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f"),
    ModelFile("embedding_model.onnx", "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f"),
)
PRETRAINED = {  # wake phrase -> model
    "hey jarvis": ModelFile("hey_jarvis_v0.1.onnx", "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb"),
}


def model_phrase(wake_word: str) -> str | None:
    """The ready-made model for a wake phrase: "hey jarvis" for itself, and also for a bare "jarvis" -
    the model scores most spoken "Jarvis" highly too; the uncertain ones are double-checked by the listener."""
    phrase = " ".join((wake_word or "").lower().split())
    if phrase in PRETRAINED:
        return phrase
    return f"hey {phrase}" if f"hey {phrase}" in PRETRAINED else None


def is_bare_name(wake_word: str) -> bool:
    phrase = " ".join((wake_word or "").lower().split())
    return phrase not in PRETRAINED and model_phrase(phrase) is not None


FRAME_SAMPLES = 1280  # 80 ms at 16 kHz
_MEL_CONTEXT = 160 * 3  # extra samples the mel model needs before each frame
_MEL_WINDOW, _MEL_MAX = 76, 970
_FEATURE_MAX = 120
_WARMUP_FRAMES = 5  # openWakeWord ignores scores until its buffers hold real audio


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_files_for(wake_word: str, custom_model: Path | None = None) -> list[ModelFile]:
    """Files needed for this wake phrase ([] entries for a custom model are checked separately)."""
    files = list(FEATURE_MODELS)
    if custom_model is None:
        model = PRETRAINED.get(model_phrase(wake_word) or "")
        if model is None:
            raise WakeWordUnavailableError(
                f"There is no ready-made wake-word model for '{wake_word}'. Supported: "
                f"{', '.join(PRETRAINED)}. Use WAKE_WORD_MODEL for a custom openWakeWord model, "
                "or WAKE_WORD_ENGINE=whisper.")
        files.append(model)
    return files


def missing_models(model_dir: Path, wake_word: str, custom_model: Path | None = None) -> list[ModelFile]:
    return [f for f in model_files_for(wake_word, custom_model)
            if not (model_dir / f.name).is_file() or (model_dir / f.name).stat().st_size == 0]


def download_models(model_dir: Path, wake_word: str, custom_model: Path | None = None, *,
                    transport: Any = None) -> list[str]:
    """Download the missing model files (about 4 MB), verifying each file's SHA-256. Returns what was fetched."""
    import httpx

    fetched = []
    model_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0), transport=transport) as client:
        for model in missing_models(model_dir, wake_word, custom_model):
            target = model_dir / model.name
            partial = target.with_name(target.name + ".part")
            try:
                response = client.get(model.url)
                response.raise_for_status()
                if hashlib.sha256(response.content).hexdigest() != model.sha256:
                    raise WakeWordUnavailableError(
                        f"The downloaded {model.name} does not match its expected checksum, so it was discarded.")
                partial.write_bytes(response.content)
                os.replace(partial, target)
                fetched.append(model.name)
                log_event(_log, "wake_model_downloaded", file=model.name)
            except httpx.HTTPError as exc:
                raise WakeWordUnavailableError(f"Downloading the wake-word model failed: {exc}") from exc
            finally:
                partial.unlink(missing_ok=True)
    return fetched


class OnnxWakeWordDetector(WakeWordDetector):
    """openWakeWord's streaming pipeline, re-implemented on onnxruntime alone.

    Per 80 ms frame: mel spectrogram of the newest audio -> one 96-value speech
    embedding of the last 76 mel frames -> the wake model scores the last N
    embeddings (N = 16 for the pre-trained models).
    """

    name = "openwakeword"
    frame_samples = FRAME_SAMPLES

    def __init__(self, model_dir: Path, wake_word: str = "hey jarvis", *, threshold: float = 0.5,
                 custom_model: Path | None = None, verify: bool = True) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise WakeWordUnavailableError("onnxruntime is not installed (pip install -r requirements.txt).") from exc
        files = model_files_for(wake_word, custom_model)
        missing = missing_models(model_dir, wake_word, custom_model)
        if missing:
            raise WakeWordUnavailableError(
                "The wake-word model is not downloaded yet. Run 'python run.py --setup-voice'.")
        if verify:
            for model in files:
                if sha256_of(model_dir / model.name) != model.sha256:
                    raise WakeWordUnavailableError(f"{model.name} is damaged or was changed. Delete it and run "
                                                   "'python run.py --setup-voice' again.")
        wake_path = custom_model if custom_model is not None else model_dir / files[-1].name
        if not wake_path.is_file():
            raise WakeWordUnavailableError(f"The wake-word model {wake_path} does not exist.")
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        providers = ["CPUExecutionProvider"]
        try:
            self._mel = ort.InferenceSession(str(model_dir / FEATURE_MODELS[0].name), options, providers=providers)
            self._embed = ort.InferenceSession(str(model_dir / FEATURE_MODELS[1].name), options, providers=providers)
            self._wake = ort.InferenceSession(str(wake_path), options, providers=providers)
        except Exception as exc:  # onnxruntime raises its own exception types
            raise WakeWordUnavailableError(f"The wake-word model could not be loaded: {exc}") from exc
        wake_input = self._wake.get_inputs()[0]
        self._wake_input = wake_input.name
        self._window = int(wake_input.shape[1])
        self.threshold = threshold
        self.last_score = 0.0
        self.reset()

    def _melspectrogram(self, pcm: np.ndarray) -> np.ndarray:
        out = self._mel.run(None, {"input": pcm.astype(np.float32)[None, :]})[0]
        return np.squeeze(out) / 10 + 2  # the transform openWakeWord applies

    def _embeddings(self, windows: np.ndarray) -> np.ndarray:
        return self._embed.run(None, {"input_1": windows.astype(np.float32)[..., None]})[0].reshape(-1, 96)

    def reset(self) -> None:
        self._raw = np.zeros(0, dtype=np.int16)
        self._mels = np.ones((_MEL_WINDOW, 32), dtype=np.float32)
        noise = np.random.default_rng(0).integers(-1000, 1000, 16000 * 4).astype(np.int16)
        spec = self._melspectrogram(noise)
        windows = np.array([spec[i:i + _MEL_WINDOW] for i in range(0, spec.shape[0] - _MEL_WINDOW + 1, 8)])
        self._features = self._embeddings(windows)
        self._frames = 0
        self.last_score = 0.0

    def score(self, frame: np.ndarray) -> float:
        """Wake-phrase probability after this 80 ms frame (float32 samples at 16 kHz)."""
        if frame.size != FRAME_SAMPLES:
            raise ValueError(f"expected {FRAME_SAMPLES} samples, got {frame.size}")
        pcm = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16)
        self._raw = np.concatenate((self._raw, pcm))[-(FRAME_SAMPLES + _MEL_CONTEXT):]
        self._mels = np.vstack((self._mels, self._melspectrogram(self._raw)))[-_MEL_MAX:]
        embedding = self._embeddings(self._mels[-_MEL_WINDOW:][None, :, :])
        self._features = np.vstack((self._features, embedding))[-_FEATURE_MAX:]
        features = self._features[-self._window:][None, :, :].astype(np.float32)
        value = float(np.asarray(self._wake.run(None, {self._wake_input: features})[0]).reshape(-1)[0])
        self._frames += 1
        self.last_score = value if self._frames > _WARMUP_FRAMES else 0.0
        return self.last_score

    def process(self, frame: np.ndarray) -> bool:
        return self.score(frame) >= self.threshold


WAKE_WORD_ENGINES: dict[str, Callable[..., WakeWordDetector]] = {"openwakeword": OnnxWakeWordDetector}


def create_wake_word_detector(wake_word: str, engine: str | None = None, **options: Any) -> WakeWordDetector:
    factory = WAKE_WORD_ENGINES.get(engine or "openwakeword")
    if factory is None:
        raise WakeWordUnavailableError(f"Wake-word engine '{engine}' is not available.")
    return factory(wake_word=wake_word, **options)


# What small speech models write for a spoken "hey" (e.g. "As Jarvis", "Hi Jarvis").
_GREETING_VARIANTS = {"hey": {"hey", "hi", "hay", "hei", "a", "ay", "eh", "as", "ask", "hello", "ok", "okay", "yo"}}
_WORD = re.compile(r"[A-Za-z0-9']+")


_PERSONAL_ALIASES: frozenset[str] = frozenset()   # spellings YOU approved with `--wake-doctor` (data\wake_aliases.json)


def set_personal_aliases(aliases) -> None:
    global _PERSONAL_ALIASES
    _PERSONAL_ALIASES = frozenset(str(a).lower() for a in aliases)


def _similar(a: str, b: str) -> bool:
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.75


def _name_matches(spoken: str, expected: str, is_name: bool) -> bool:
    return _similar(spoken, expected) or (is_name and spoken in _PERSONAL_ALIASES)


def strip_wake_phrase(text: str, wake_word: str) -> tuple[str, bool]:
    """Return (text without a leading wake phrase, whether it was present)."""
    wake = wake_word.lower().split()
    if not wake:
        return text.strip(), False
    words = list(_WORD.finditer(text))
    candidates = [wake]
    if wake[0] in _GREETING_VARIANTS and len(wake) > 1:
        candidates.append(wake[1:])  # "Jarvis, open Chrome" (greeting dropped)
    for candidate in candidates:
        if len(words) < len(candidate):
            continue
        ok = True
        for position, (spoken, expected) in enumerate(zip(words, candidate)):
            s = spoken.group().lower()
            if expected in _GREETING_VARIANTS and s in _GREETING_VARIANTS[expected]:
                continue
            if not _name_matches(s, expected, position == len(candidate) - 1):
                ok = False
                break
        if ok:
            rest = text[words[len(candidate) - 1].end():]
            return rest.lstrip(" ,.!?;:-").strip(), True
    from app.agent.language import strip_bangla_wake

    return strip_bangla_wake(text)  # "জার্ভিস, ক্রোম খোলো"


# Sounds that are not requests: "mm", "hmm", "uh"... (and what the speech model
# writes for background noise). Acting on them made the model repeat its last action.
_FILLERS = frozenset({"mm", "mmm", "mmmm", "hmm", "hm", "hmmm", "mhm", "mmhmm", "mm-hmm", "mm-mm", "uh", "uhh", "um",
                      "umm", "uhm", "ah", "ahh", "oh", "ohh", "eh", "er", "erm", "huh", "uh-huh", "uh-oh", "ha", "haha",
                      "hm-hmm", "shh", "psst", "ooh", "aah", "hmph"})
# What Whisper typically "hears" in silence or noise when nobody asked it anything.
_NOISE_PHRASES = frozenset({"you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye",
                            "okay", "ok", "so", "the", "i", "yeah", "please subscribe", "subtitles by", "music",
                            "thank you thank you", "thank you so much for watching", "see you next time",
                            "thanks for listening", "subscribe", "like and subscribe"})


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+(?:-[a-z]+)*", text.lower())]


def is_filler(text: str) -> bool:
    """Only filler sounds ("Mm-hmm, mm.", "Uh...")."""
    words = _words(text)
    return not words or all(w in _FILLERS or re.fullmatch(r"(m+|h+m+|u+h*m*|a+h+|o+h+)", w) for w in words)


def is_background_noise(text: str) -> bool:
    """Fillers, or a phrase speech recognition invents from noise ("Thank you.", "you")."""
    return is_filler(text) or " ".join(_words(text)) in _NOISE_PHRASES
