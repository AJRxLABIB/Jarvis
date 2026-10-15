"""Offline Bangla voice: a VITS model (Coqui "bn custom female", via sherpa-onnx), on this PC.

Used for Bangla sentences whenever the neural online voice isn't available (offline, or
TTS_ENGINE=auto), so Bangla replies are always spoken. About 108 MB, downloaded once and
checked against a pinned SHA-256. English sentences keep using Piper / the Windows voice.
"""

from __future__ import annotations

import hashlib
import re
import tarfile
import threading
from pathlib import Path

import numpy as np

from app.tts.base import AudioClip, TTSEngine, TTSError
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tts.bangla")
ARCHIVE_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
               "vits-coqui-bn-custom_female.tar.bz2")
ARCHIVE_SHA256 = "a03292d7da03650e892bb1989b40dc2c62574c0d6c34c8bef185fbb3151417a1"
MODEL_SHA256 = "8d64b23b4069feded76fda6543849516947d06db8dae3d90109e67d12c402323"
FILES = ("model.onnx", "tokens.txt")
BENGALI = re.compile(r"[ঀ-৿]")


def voice_dir(models_dir: Path) -> Path:
    return Path(models_dir) / "tts-bn"


def is_installed(models_dir: Path) -> bool:
    folder = voice_dir(models_dir)
    return all((folder / name).is_file() for name in FILES)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(models_dir: Path, on_progress=None) -> Path:
    """Download and unpack the voice (only model.onnx and tokens.txt are extracted)."""
    import httpx

    folder = voice_dir(models_dir)
    if is_installed(models_dir):
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "voice.tar.bz2.part"
    done = archive.stat().st_size if archive.exists() else 0
    headers = {"Range": f"bytes={done}-"} if done else {}
    try:
        with httpx.stream("GET", ARCHIVE_URL, follow_redirects=True, timeout=60.0, headers=headers) as response:
            if response.status_code not in (200, 206):
                response.raise_for_status()
            mode = "ab" if response.status_code == 206 else "wb"
            total = int(response.headers.get("content-length") or 0) + (done if mode == "ab" else 0)
            with archive.open(mode) as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
                    if on_progress and total:
                        on_progress(out.tell(), total)
    except httpx.HTTPError as exc:
        raise TTSError(f"Could not download the Bangla voice: {exc} (it continues next time)") from exc
    if _sha256(archive) != ARCHIVE_SHA256:
        archive.unlink(missing_ok=True)
        raise TTSError("The downloaded Bangla voice failed its checksum; it was deleted.")
    with tarfile.open(archive, "r:bz2") as tar:
        for member in tar.getmembers():
            name = Path(member.name).name
            if member.isfile() and name in FILES:  # nothing else, and never a path outside the folder
                with tar.extractfile(member) as source, (folder / name).open("wb") as target:
                    target.write(source.read())
    archive.unlink(missing_ok=True)
    if _sha256(folder / "model.onnx") != MODEL_SHA256:
        raise TTSError("The Bangla voice model is damaged; delete data\\models\\tts-bn and try again.")
    log_event(_log, "bangla_voice_installed")
    return folder


class BanglaTTS(TTSEngine):
    name = "bangla-offline"

    def __init__(self, models_dir: Path, *, factory=None) -> None:
        self.models_dir = Path(models_dir)
        self._factory = factory
        self._tts = None
        self._lock = threading.Lock()

    @property
    def description(self) -> str:
        return "Offline Bangla voice"

    def load(self) -> None:
        with self._lock:
            if self._tts is not None:
                return
            if self._factory is not None:
                self._tts = self._factory()
                return
            if not is_installed(self.models_dir):
                raise TTSError("The offline Bangla voice is not downloaded yet.")
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise TTSError("The offline Bangla voice needs sherpa-onnx: pip install -r requirements.txt") from exc
            folder = voice_dir(self.models_dir)
            config = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(folder / "model.onnx"),
                                                           tokens=str(folder / "tokens.txt")),
                num_threads=2))
            self._tts = sherpa_onnx.OfflineTts(config)

    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        self.load()
        audio = self._tts.generate(text, sid=0, speed=speed)
        return AudioClip(np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate))


class BilingualTTS(TTSEngine):
    """Bangla sentences -> the Bangla voice, everything else -> the English engine."""

    name = "bilingual"
    speaks_bangla = True

    def __init__(self, english: TTSEngine, bangla: TTSEngine) -> None:
        self.english, self.bangla = english, bangla

    @property
    def description(self) -> str:
        return f"{self.english.description} + offline Bangla voice"

    def load(self) -> None:
        self.english.load()
        try:
            self.bangla.load()
        except TTSError as exc:
            log_event(_log, "bangla_voice_unavailable", reason=str(exc))

    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        if BENGALI.search(text):
            try:
                return self.bangla.synthesize(text, speed)
            except Exception as exc:
                log_event(_log, "bangla_voice_failed", detail=str(exc)[:160])
                return AudioClip(np.zeros(0, np.float32), 22050)
        return self.english.synthesize(text, speed)

    def close(self) -> None:
        self.english.close()
