"""Piper neural TTS (local, offline after the voice is downloaded).

Voices come from https://huggingface.co/rhasspy/piper-voices and are stored in
``data/models/piper``. Note: the piper-tts package is GPL-3.0 licensed.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from app.tts.base import AudioClip, TTSEngine, TTSError
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tts.piper")
VOICE_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "{family}/{code}/{name}/{quality}/{code}-{name}-{quality}{ext}?download=true"
)
_VOICE_ID = re.compile(r"^(?P<code>(?P<family>[a-z]{2,3})_[A-Z]{2})-(?P<name>[A-Za-z0-9_]+)-(?P<quality>x_low|low|medium|high)$")

ProgressCallback = Callable[[str, int, int | None], None]


def parse_voice_id(voice_id: str) -> dict[str, str]:
    match = _VOICE_ID.match(voice_id)
    if not match:
        raise TTSError(f"'{voice_id}' is not a valid Piper voice id (example: en_US-lessac-medium).")
    return match.groupdict()


def voice_files(voice_dir: Path, voice_id: str) -> tuple[Path, Path]:
    parse_voice_id(voice_id)
    return voice_dir / f"{voice_id}.onnx", voice_dir / f"{voice_id}.onnx.json"


def is_voice_installed(voice_dir: Path, voice_id: str) -> bool:
    return all(p.is_file() and p.stat().st_size > 0 for p in voice_files(voice_dir, voice_id))


def download_voice(
    voice_id: str,
    voice_dir: Path,
    *,
    progress: ProgressCallback | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Download a voice (model + config). Existing files are kept. Returns the .onnx path."""
    parts = parse_voice_id(voice_id)
    voice_dir.mkdir(parents=True, exist_ok=True)
    model_path, config_path = voice_files(voice_dir, voice_id)
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0), transport=transport) as client:
        for target, ext in ((config_path, ".onnx.json"), (model_path, ".onnx")):
            if target.is_file() and target.stat().st_size > 0:
                continue
            url = VOICE_URL.format(ext=ext, **parts)
            partial = target.with_name(target.name + ".part")
            log_event(_log, "voice_download_started", voice=voice_id, file=target.name)
            try:
                with client.stream("GET", url) as response:
                    if response.status_code == 404:
                        raise TTSError(f"The Piper voice '{voice_id}' does not exist.")
                    response.raise_for_status()
                    total = int(response.headers.get("content-length", 0)) or None
                    done = 0
                    with open(partial, "wb") as handle:
                        for chunk in response.iter_bytes(1 << 16):
                            handle.write(chunk)
                            done += len(chunk)
                            if progress is not None:
                                progress(target.name, done, total)
                os.replace(partial, target)
            except httpx.HTTPError as exc:
                raise TTSError(f"Downloading the Piper voice failed: {exc}") from exc
            finally:
                if partial.exists():
                    partial.unlink(missing_ok=True)
    log_event(_log, "voice_downloaded", voice=voice_id)
    return model_path


class PiperTTS(TTSEngine):
    name = "piper"

    def __init__(
        self, voice_id: str, voice_dir: Path, *, use_cuda: bool = False, loader: Callable[..., Any] | None = None
    ) -> None:
        self.voice_id = voice_id
        self.voice_dir = Path(voice_dir)
        self.use_cuda = use_cuda
        self._loader = loader
        self._voice: Any = None
        self._config_cls: Any = None
        self._lock = threading.Lock()

    @property
    def description(self) -> str:
        return f"Piper ({self.voice_id})"

    def load(self) -> None:
        with self._lock:
            if self._voice is not None:
                return
            model_path, config_path = voice_files(self.voice_dir, self.voice_id)
            if not is_voice_installed(self.voice_dir, self.voice_id):
                raise TTSError(
                    f"The Piper voice '{self.voice_id}' is not downloaded. Run 'python run.py --setup-voice'."
                )
            loader = self._loader
            if loader is None:
                try:
                    from piper import PiperVoice, SynthesisConfig
                except ImportError as exc:
                    raise TTSError("Piper is not installed. Run 'pip install -r requirements.txt'.") from exc
                loader, self._config_cls = PiperVoice.load, SynthesisConfig
            try:
                self._voice = loader(str(model_path), config_path=str(config_path), use_cuda=self.use_cuda)
            except Exception as exc:
                _log.warning("piper voice load failed", exc_info=True)
                raise TTSError(f"The Piper voice '{self.voice_id}' could not be loaded: {exc}") from exc
            log_event(_log, "piper_loaded", voice=self.voice_id)

    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        self.load()
        config = self._config_cls(length_scale=1.0 / max(speed, 0.1)) if self._config_cls else None
        try:
            with self._lock:
                chunks = list(self._voice.synthesize(text, syn_config=config))
        except Exception as exc:
            _log.warning("piper synthesis failed", exc_info=True)
            raise TTSError("Speech synthesis failed. The details are in the log.") from exc
        if not chunks:
            return AudioClip(np.zeros(0, dtype=np.float32), 22050)
        samples = np.concatenate([np.asarray(c.audio_float_array, dtype=np.float32) for c in chunks])
        return AudioClip(samples, int(chunks[0].sample_rate))

    def voices(self) -> list[str]:
        if not self.voice_dir.is_dir():
            return []
        return sorted(p.name[: -len(".onnx")] for p in self.voice_dir.glob("*.onnx"))
