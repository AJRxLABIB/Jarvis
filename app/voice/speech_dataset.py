"""Your own speech, kept to make the recognizer better at YOUR voice and language (Bangla especially).

Off by default (SPEECH_DATASET=off). When on, voice commands are kept on this PC as 16 kHz WAV + the right text:
* ``corrections`` - only when you corrected what was heard ("no, I said ..."): the audio + the corrected text. These
  are the most valuable examples (exactly what the recognizer got wrong);
* ``all`` - also every confident voice command (labelled "unverified": review before training).

Nothing leaves the PC. Folder: data/speech_dataset/<language>/ (clips/*.wav + metadata.jsonl). Limits: MAX_CLIPS per
language, MAX_SECONDS per clip. "forget my voice recordings" (or deleting the folder) erases it.

``export`` writes a Hugging Face "audiofolder" (clips + metadata.csv) for scripts/finetune_whisper.py, which
fine-tunes Whisper on a GPU and converts it for faster-whisper; then STT_BANGLA_MODEL=<that folder> uses it.
"""

from __future__ import annotations

import csv
import json
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

FOLDER = "speech_dataset"
RATE = 16000
MAX_CLIPS = 5000
MAX_SECONDS = 20.0
MIN_SECONDS = 0.4
MODES = ("off", "corrections", "all")
BANGLA = range(0x0980, 0x0A00)


def is_bangla(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and sum(ord(ch) in BANGLA for ch in letters) / len(letters) >= 0.5


def bangla_hints(words, limit: int = 40) -> str | None:
    """Bangla-script words for the recognizer's Bangla hint (the English hint would pull Bangla towards English)."""
    out: list[str] = []
    for word in words:
        word = " ".join(str(word).split())
        if word and is_bangla(word) and word not in out:
            out.append(word)
        if len(out) >= limit:
            break
    return ", ".join(out) or None


class SpeechDataset:
    def __init__(self, data_dir: Path, clock=time.time) -> None:
        self.root = Path(data_dir) / FOLDER
        self._clock = clock

    def _dir(self, language: str) -> Path:
        safe = "".join(ch for ch in (language or "und") if ch.isalnum())[:8] or "und"
        return self.root / safe

    def count(self, language: str) -> int:
        meta = self._dir(language) / "metadata.jsonl"
        try:
            return sum(1 for line in meta.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            return 0

    def record(self, audio, text: str, language: str, source: str) -> Path | None:
        """Keep one clip. Returns its path, or None when it isn't worth keeping / the limit is reached."""
        text = " ".join((text or "").split())
        samples = np.asarray(audio if audio is not None else [], dtype=np.float32).reshape(-1)
        seconds = samples.size / RATE
        if not text or not MIN_SECONDS <= seconds <= MAX_SECONDS or self.count(language) >= MAX_CLIPS:
            return None
        folder = self._dir(language)
        (folder / "clips").mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.wav"
        path = folder / "clips" / name
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(pcm.tobytes())
        entry = {"file": f"clips/{name}", "text": text, "language": language, "source": source,
                 "seconds": round(seconds, 2),
                 "at": datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(timespec="seconds")}
        with (folder / "metadata.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path

    def entries(self, language: str) -> list[dict]:
        meta = self._dir(language) / "metadata.jsonl"
        try:
            lines = meta.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def summary(self) -> dict[str, dict]:
        result = {}
        if not self.root.is_dir():
            return result
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
            items = self.entries(folder.name)
            result[folder.name] = {"clips": len(items), "corrected": sum(e.get("source") == "corrected" for e in items),
                                   "minutes": round(sum(e.get("seconds", 0) for e in items) / 60, 1)}
        return result

    def export(self, language: str, target: Path, *, include_unverified: bool = False) -> int:
        """A Hugging Face audiofolder: target/clips/*.wav + target/metadata.csv (file_name, transcription)."""
        import shutil

        source = self._dir(language)
        rows = [e for e in self.entries(language) if include_unverified or e.get("source") == "corrected"]
        (target / "clips").mkdir(parents=True, exist_ok=True)
        written = 0
        with (target / "metadata.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["file_name", "transcription"])
            for e in rows:
                clip = source / e["file"]
                if clip.is_file():
                    shutil.copy2(clip, target / e["file"])
                    writer.writerow([e["file"], e["text"]])
                    written += 1
        return written

    def forget(self) -> int:
        import shutil

        total = sum(v["clips"] for v in self.summary().values())
        if self.root.is_dir():
            shutil.rmtree(self.root, ignore_errors=True)
        return total
