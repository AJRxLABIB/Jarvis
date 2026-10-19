"""Files and clipboard text your phone sends to this PC (JARVIS Mobile: Share > JARVIS Mobile, "Send clipboard").

* Files land in data/phone_inbox/<date>/ under a cleaned-up name (never a path the phone chose, never executed,
  never overwriting another file). At most MAX_FILE_BYTES each and MAX_DAY_BYTES a day per phone.
* Clipboard text goes onto the PC's clipboard (the window does it) and is kept as a notification (codes hidden).
"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from pathlib import Path

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_DAY_BYTES = 300 * 1024 * 1024
FOLDER = "phone_inbox"
_UNSAFE = re.compile(r"[^\w.\- ()ঀ-৿]+")


class InboxError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def safe_name(name: str) -> str:
    """'../../evil.exe' -> 'evil.exe'; empty -> 'file'. Only a plain file name survives."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = _UNSAFE.sub("_", base).strip(" ._") or "file"
    stem, dot, ext = base.rpartition(".")
    if dot and len(ext) <= 10:
        return f"{stem[:80] or 'file'}.{ext}"
    return base[:90]


class PhoneInbox:
    def __init__(self, data_dir: Path, clock=datetime.now) -> None:
        self.root = Path(data_dir) / FOLDER
        self._clock = clock

    def _today(self) -> Path:
        return self.root / self._clock().strftime("%Y-%m-%d")

    def used_today(self) -> int:
        folder = self._today()
        return sum(p.stat().st_size for p in folder.iterdir() if p.is_file()) if folder.is_dir() else 0

    def save(self, name: str, data_b64: str) -> Path:
        if not isinstance(data_b64, str) or not data_b64:
            raise InboxError("malformed", "no file")
        try:
            data = base64.b64decode(data_b64, validate=True)
        except ValueError:
            raise InboxError("malformed", "the file isn't base64") from None
        if not data:
            raise InboxError("malformed", "empty file")
        if len(data) > MAX_FILE_BYTES:
            raise InboxError("too_big", f"files up to {MAX_FILE_BYTES // (1024 * 1024)} MB")
        if self.used_today() + len(data) > MAX_DAY_BYTES:
            raise InboxError("quota", "today's limit for files from the phone is reached")
        folder = self._today()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / safe_name(name)
        stem, suffix, n = target.stem, target.suffix, 1
        while target.exists():                      # never overwrite: photo.jpg, photo (2).jpg, ...
            n += 1
            target = folder / f"{stem} ({n}){suffix}"
        target.write_bytes(data)
        return target

    def recent(self, limit: int = 10) -> list[Path]:
        if not self.root.is_dir():
            return []
        files = [p for p in self.root.rglob("*") if p.is_file()]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
