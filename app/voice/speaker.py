"""Speaker verification: only the enrolled voice can give commands (SPEAKER_VERIFICATION).

A small neural speaker-embedding model (WeSpeaker ResNet34, trained on VoxCeleb, ONNX, 27 MB)
turns an utterance into a 256-number "voice print". Enrollment averages a few recordings of
you; afterwards each command is compared with it (cosine similarity). The model is downloaded
once and checked against a pinned SHA-256. Voice prints stay on this PC (data/voiceprint.npy).
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import numpy as np

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("speaker")
MODEL_NAME = "wespeaker_en_voxceleb_resnet34.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "wespeaker_en_voxceleb_resnet34.onnx")
MODEL_SHA256 = "5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94"
VOICEPRINT_FILE = "voiceprint.npy"
SAMPLE_RATE = 16000
MIN_SECONDS = 0.8  # shorter than this says too little about a voice: accepted without checking


class SpeakerError(Exception):
    """User-readable problem with speaker verification."""


# ------------------------------------------------------------------ features (Kaldi-style fbank)

_MEL_CACHE: dict[tuple, np.ndarray] = {}


def _mel_filters(n_mels: int = 80, n_fft: int = 512, rate: int = SAMPLE_RATE, low: float = 20.0) -> np.ndarray:
    key = (n_mels, n_fft, rate, low)
    if key in _MEL_CACHE:
        return _MEL_CACHE[key]

    def mel(f):
        return 1127.0 * np.log(1.0 + np.asarray(f) / 700.0)

    high = rate / 2
    mel_points = np.linspace(mel(low), mel(high), n_mels + 2)
    bins = mel(np.arange(n_fft // 2 + 1) * rate / n_fft)
    filters = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for m in range(n_mels):
        left, centre, right = mel_points[m], mel_points[m + 1], mel_points[m + 2]
        up = (bins - left) / (centre - left)
        down = (right - bins) / (right - centre)
        filters[m] = np.maximum(0.0, np.minimum(up, down))
    _MEL_CACHE[key] = filters
    return filters


def fbank(audio: np.ndarray, n_mels: int = 80) -> np.ndarray:
    """80-dim log mel filterbank, 25 ms frames every 10 ms, with per-utterance mean removal."""
    wave = np.asarray(audio, dtype=np.float64).reshape(-1) * 32768.0  # the model expects 16-bit scale
    frame, hop, n_fft = 400, 160, 512
    if wave.size < frame:
        wave = np.pad(wave, (0, frame - wave.size))
    count = 1 + (wave.size - frame) // hop
    index = np.arange(frame)[None, :] + hop * np.arange(count)[:, None]
    frames = wave[index]
    frames = frames - frames.mean(axis=1, keepdims=True)  # remove DC
    frames = np.concatenate([frames[:, :1] * (1 - 0.97), frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)
    frames *= np.hamming(frame)[None, :]
    power = np.abs(np.fft.rfft(frames, n=n_fft)) ** 2
    feats = np.log(np.maximum(power @ _mel_filters(n_mels, n_fft).T, np.finfo(np.float32).eps))
    feats -= feats.mean(axis=0, keepdims=True)
    return feats.astype(np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a, b = np.asarray(a, np.float32).ravel(), np.asarray(b, np.float32).ravel()
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


# ------------------------------------------------------------------ model


def model_path(model_dir: Path) -> Path:
    return Path(model_dir) / MODEL_NAME


def download_model(model_dir: Path) -> Path:
    import httpx

    path = model_path(model_dir)
    if path.is_file() and _sha256(path) == MODEL_SHA256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    try:
        with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=60.0) as response:
            response.raise_for_status()
            with partial.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
    except httpx.HTTPError as exc:
        raise SpeakerError(f"Could not download the voice-print model: {exc}") from exc
    if _sha256(partial) != MODEL_SHA256:
        partial.unlink(missing_ok=True)
        raise SpeakerError("The downloaded voice-print model failed its checksum; it was deleted.")
    partial.replace(path)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class SpeakerVerifier:
    """Voice prints: ``enroll`` recordings of the owner, then ``verify`` each command."""

    def __init__(self, model_dir: Path, data_dir: Path, *, threshold: float = 0.45, session=None) -> None:
        self.model_dir = Path(model_dir)
        self.data_dir = Path(data_dir)
        self.threshold = threshold
        self._session = session
        self._lock = threading.Lock()
        self._voiceprint: np.ndarray | None = None
        self.last_score: float | None = None

    @property
    def voiceprint_path(self) -> Path:
        return self.data_dir / VOICEPRINT_FILE

    @property
    def enrolled(self) -> bool:
        return self._load_voiceprint() is not None

    def _load_voiceprint(self) -> np.ndarray | None:
        if self._voiceprint is None and self.voiceprint_path.is_file():
            try:
                import io

                from app.security.protect import read_bytes

                self._voiceprint = np.load(io.BytesIO(read_bytes(self.voiceprint_path)), allow_pickle=False)
            except Exception:
                self._voiceprint = None
        return self._voiceprint

    def _model(self):
        with self._lock:
            if self._session is None:
                path = model_path(self.model_dir)
                if not path.is_file():
                    raise SpeakerError("The voice-print model is not downloaded. Use Menu -> Enroll my voice.")
                import onnxruntime

                opts = onnxruntime.SessionOptions()
                opts.log_severity_level = 4
                opts.intra_op_num_threads = 2
                self._session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"],
                                                             sess_options=opts)
            return self._session

    def embed(self, audio: np.ndarray) -> np.ndarray:
        feats = fbank(audio)[None, :, :]
        session = self._model()
        name = session.get_inputs()[0].name
        return np.asarray(session.run(None, {name: feats})[0], dtype=np.float32).reshape(-1)

    def enroll(self, recordings: list[np.ndarray]) -> np.ndarray:
        usable = [r for r in recordings if r.size >= SAMPLE_RATE * MIN_SECONDS]
        if len(usable) < 2:
            raise SpeakerError("I need at least two clear recordings of your voice (a few seconds each).")
        embeddings = np.stack([self.embed(r) for r in usable])
        embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-9
        voiceprint = embeddings.mean(axis=0)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        import io

        from app.security.protect import write_bytes

        buffer = io.BytesIO()
        np.save(buffer, voiceprint)
        write_bytes(self.voiceprint_path, buffer.getvalue())  # encrypted for your Windows account
        self._voiceprint = voiceprint
        spread = min(cosine(e, voiceprint) for e in embeddings)
        log_event(_log, "voice_enrolled", recordings=len(usable), consistency=round(spread, 3))
        return voiceprint

    def forget(self) -> None:
        self._voiceprint = None
        self.voiceprint_path.unlink(missing_ok=True)

    def verify(self, audio: np.ndarray) -> bool:
        """True if the voice is the owner's (or can't be judged: too short / not enrolled)."""
        voiceprint = self._load_voiceprint()
        if voiceprint is None or audio.size < SAMPLE_RATE * MIN_SECONDS:
            self.last_score = None
            return True
        score = cosine(self.embed(audio), voiceprint)
        self.last_score = score
        ok = score >= self.threshold
        log_event(_log, "speaker_checked", accepted=ok, score=round(score, 3))  # the score only, never audio
        return ok
