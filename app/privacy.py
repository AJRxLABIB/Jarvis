"""Privacy dashboard: everything Jarvis keeps about you on this PC, what it is, and a way to delete it.

Each item says how much is stored, shows a short preview on request, and can be erased. Nothing here
leaves the PC. Secrets are never shown: the phone-remote key is only renewed, never displayed.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover
    from app.main import AppContext

PREVIEW_LINES = 40


@dataclass(frozen=True)
class DataItem:
    key: str
    title: str
    description: str
    count: int  # rows / entries / files (0 = nothing stored)
    size: int = 0  # bytes on disk, for files and folders

    @property
    def summary(self) -> str:
        if not self.count:
            return "nothing stored"
        text = f"{self.count} item{'s' if self.count != 1 else ''}"
        return f"{text}, {human_size(self.size)}" if self.size else text


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"  # pragma: no cover


def _files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return [p for p in path.rglob("*") if p.is_file()]
    return []


def _size(path: Path) -> int:
    total = 0
    for f in _files(path):
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def _json_len(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return len(data) if isinstance(data, (dict, list)) else 0


# ------------------------------------------------------------------ the items

@dataclass(frozen=True)
class _Table:
    title: str
    description: str
    sql_count: str
    sql_preview: str
    sql_delete: tuple[str, ...]


TABLES: dict[str, _Table] = {
    "memories": _Table("Memories", "Facts you asked it to remember (\"remember my project folder is ...\").",
                       "SELECT COUNT(*) FROM memories",
                       "SELECT content FROM memories ORDER BY updated_at DESC LIMIT ?",
                       ("DELETE FROM memories",)),
    "suggestions": _Table("Memory suggestions", "Facts it noticed and offered to remember (accepted, rejected or waiting).",
                          "SELECT COUNT(*) FROM memory_suggestions",
                          "SELECT status || ': ' || text FROM memory_suggestions ORDER BY id DESC LIMIT ?",
                          ("DELETE FROM memory_suggestions",)),
    "history": _Table("Conversation history", "Past conversations kept for \"what did we talk about\" (secrets redacted).",
                      "SELECT (SELECT COUNT(*) FROM semantic_items WHERE kind = 'history') + "
                      "(SELECT COUNT(*) FROM conversations)",
                      "SELECT substr(text, 1, 160) FROM semantic_items WHERE kind = 'history' ORDER BY id DESC LIMIT ?",
                      ("DELETE FROM semantic_items WHERE kind = 'history'", "DELETE FROM conversations")),
    "documents": _Table("Document index", "Searchable pieces of the documents in your document folders.",
                        "SELECT COUNT(*) FROM semantic_items WHERE kind = 'document'",
                        "SELECT source || '  (' || COUNT(*) || ' parts)' FROM semantic_items WHERE kind = 'document' "
                        "GROUP BY source ORDER BY source LIMIT ?",
                        ("DELETE FROM semantic_items WHERE kind = 'document'",)),
    "habits": _Table("Habits", "Which apps you open at what time, for \"you usually open ...\" suggestions (30 days).",
                     "SELECT COUNT(*) FROM usage_events",
                     "SELECT name || ': ' || COUNT(*) || ' times' FROM usage_events GROUP BY name "
                     "ORDER BY COUNT(*) DESC LIMIT ?",
                     ("DELETE FROM usage_events",)),
    "activity": _Table("Activity log", "Which tools ran and what was allowed or refused (arguments redacted).",
                       "SELECT COUNT(*) FROM audit_log",
                       "SELECT ts || '  ' || event || '  ' || COALESCE(tool, '') FROM audit_log ORDER BY id DESC LIMIT ?",
                       ("DELETE FROM audit_log",)),
}

# key -> (title, description, relative path in the data folder, counter)
FILES: dict[str, tuple[str, str, str, Callable[[Path], int]]] = {
    "contacts": ("Contacts", "Names and numbers for \"call Ammu\".", "contacts.json", _json_len),
    "corrections": ("Learned corrections", "Words it misheard and what you meant (\"no, I said ...\").",
                    "corrections.json", _json_len),
    "voiceprint": ("Voice print", "Numbers describing your voice, for \"only my voice\". No audio is kept.",
                   "voiceprint.npy", lambda p: int(p.is_file())),
    "wake_templates": ("Wake-word recordings", "Numbers describing how you say the wake phrase. No audio is kept.",
                       "wake_templates.npz", lambda p: int(p.is_file())),
    "tone": ("Speaking habits", "Your usual speaking speed and loudness (two numbers), for tone awareness.",
             "tone.json", lambda p: int(p.is_file())),
    "benchmark": ("Benchmark recordings", "Your recorded test commands (WAV files) for the voice benchmark.",
                  "benchmark", lambda p: len([f for f in _files(p) if f.suffix == ".wav"])),
    "speed": ("Speed timings", "How long hearing, thinking and speaking took (numbers only, never what was said).",
              "speed.json", _json_len),
    "crash_reports": ("Crash reports", "Diagnostics saved after a crash (settings and logs, secrets redacted).",
                      "crash-reports", lambda p: len(_files(p))),
}


def _data_dir(ctx: "AppContext") -> Path:
    return Path(ctx.paths.data_dir)


def _log_dir(ctx: "AppContext") -> Path:
    return Path(ctx.log_file).parent


def inventory(ctx: "AppContext") -> list[DataItem]:
    items: list[DataItem] = []
    with ctx.database.connect() as conn:
        for key, table in TABLES.items():
            try:
                count = int(conn.execute(table.sql_count).fetchone()[0])
            except Exception:
                count = 0
            items.append(DataItem(key, table.title, table.description, count))
    for key, (title, description, name, counter) in FILES.items():
        path = _data_dir(ctx) / name
        items.append(DataItem(key, title, description, counter(path), _size(path)))
    logs = [f for f in _files(_log_dir(ctx)) if f != Path(ctx.log_file)]
    items.append(DataItem("logs", "Old log files", "Earlier logs (secrets are never written). Today's log stays.",
                          len(logs), sum(f.stat().st_size for f in logs)))
    return items


def preview(ctx: "AppContext", key: str, limit: int = PREVIEW_LINES) -> str:
    """A short, readable look at what is stored (never secrets)."""
    if key in TABLES:
        with ctx.database.connect() as conn:
            rows = [str(r[0]) for r in conn.execute(TABLES[key].sql_preview, (limit,))]
        return "\n".join(rows) or "Nothing stored."
    if key in ("contacts", "corrections"):
        path = _data_dir(ctx) / FILES[key][2]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "Nothing stored."
        arrow = ": " if key == "contacts" else "  ->  "
        lines = [f"{k}{arrow}{v}" for k, v in list(data.items())[:limit]] if isinstance(data, dict) else []
        return "\n".join(lines) or "Nothing stored."
    if key == "voiceprint":
        return "A voice print is stored (numbers only)." if (_data_dir(ctx) / "voiceprint.npy").is_file() \
            else "No voice print. Menu -> Enroll my voice to make one."
    if key in FILES or key == "logs":
        root = _log_dir(ctx) if key == "logs" else _data_dir(ctx) / FILES[key][2]
        names = [f"{f.name}  ({human_size(f.stat().st_size)})" for f in sorted(_files(root))[:limit]]
        return "\n".join(names) or "Nothing stored."
    raise KeyError(key)


def erase(ctx: "AppContext", key: str) -> str:
    """Delete one kind of data. Returns what was done, for the notice."""
    if key in TABLES:
        with ctx.database.transaction() as conn:
            for statement in TABLES[key].sql_delete:
                conn.execute(statement)
        return f"{TABLES[key].title} deleted."
    if key == "logs":
        current = Path(ctx.log_file)
        removed = 0
        for f in _files(_log_dir(ctx)):
            if f == current:
                continue
            try:
                f.unlink()
                removed += 1
            except OSError:  # a log still open elsewhere
                pass
        return f"{removed} old log file(s) deleted."
    if key in FILES:
        path = _data_dir(ctx) / FILES[key][2]
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return f"{FILES[key][0]} deleted."
    raise KeyError(key)


def renew_remote_key(ctx: "AppContext") -> None:
    """Forget every paired phone: each one has to pair again with a new code."""
    from app.remote.pairing import PairingStore

    PairingStore(_data_dir(ctx)).remove_all()
    (_data_dir(ctx) / "remote_token.txt").unlink(missing_ok=True)  # the pre-3.4 key in the address
