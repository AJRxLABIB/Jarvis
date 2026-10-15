"""Kokoro: a natural-sounding English voice that works offline (TTS_ENGINE=kokoro, or automatically
when the neural online voice has no internet).

The model (92 MB, int8) and the voices (28 MB) are downloaded once into models\\kokoro and checked
against their SHA-256. Bangla sentences still go to the offline Bangla voice. Needs the package
kokoro-onnx (installed with the requirements).
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import httpx
import numpy as np

from app.tts.base import AudioClip, TTSEngine, TTSError
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tts.kokoro")
BASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {
    "kokoro-v1.0.int8.onnx": "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
DEFAULT_VOICE = "bm_george"  # British male; others: am_michael, af_heart, bf_emma ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def is_installed(model_dir: Path) -> bool:
    return all((Path(model_dir) / name).is_file() for name in FILES)


def download(model_dir: Path, on_progress=None, transport: httpx.BaseTransport | None = None) -> Path:
    """Download and verify the model files (a file that fails its checksum is deleted)."""
    folder = Path(model_dir)
    folder.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        target = folder / name
        if target.is_file() and _sha256(target) == expected:
            continue
        partial = target.with_suffix(target.suffix + ".part")
        with httpx.Client(transport=transport, timeout=60.0, follow_redirects=True) as client:
            with client.stream("GET", BASE_URL + name) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                done = 0
                with open(partial, "wb") as out:
                    for chunk in response.iter_bytes(1 << 20):
                        out.write(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            on_progress(name, done, total)
        if _sha256(partial) != expected:
            partial.unlink(missing_ok=True)
            raise TTSError(f"The Kokoro file {name} failed its checksum and was deleted.")
        partial.replace(target)
    log_event(_log, "kokoro_downloaded")
    return folder


class KokoroTTS(TTSEngine):
    name = "kokoro"

    def __init__(self, model_dir: Path, voice: str = DEFAULT_VOICE, allow_download: bool = True) -> None:
        self.model_dir = Path(model_dir)
        self.voice = voice or DEFAULT_VOICE
        self.allow_download = allow_download
        self._kokoro = None
        self._lock = threading.Lock()

    def load(self) -> None:
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            raise TTSError("The Kokoro voice needs the package kokoro-onnx.") from exc
        if not is_installed(self.model_dir):
            if not self.allow_download:
                raise TTSError("The Kokoro voice is not downloaded yet.")
            download(self.model_dir)
        try:
            self._kokoro = Kokoro(str(self.model_dir / "kokoro-v1.0.int8.onnx"), str(self.model_dir / "voices-v1.0.bin"))
        except Exception as exc:
            raise TTSError(f"The Kokoro voice could not start: {exc}") from exc

    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        if self._kokoro is None:
            self.load()
        with self._lock:
            samples, rate = self._kokoro.create(text, voice=self.voice, speed=max(0.5, min(2.0, speed)), lang="en-gb"
                                                if self.voice.startswith("b") else "en-us")
        return AudioClip(np.asarray(samples, dtype=np.float32), int(rate))

    @property
    def description(self) -> str:
        return f"Kokoro ({self.voice}, offline)"
