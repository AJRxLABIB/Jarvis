"""Retrieval sources: what may be indexed and searched, extra readers for notes/calendar/repository exports, and near-duplicates.

* EXCLUSIONS - folder or name patterns you never want indexed or returned (``retrieval.exclude`` in the settings store). Applied when
  indexing AND when answering, so a path you exclude later stops appearing immediately; ``purge_excluded`` also deletes what was
  already indexed from it.
* SOURCE SWITCHES - memories, documents, conversations can each be left out of the combined search (``retrieval.disabled``).
* ADAPTERS - plain-text readers for exports Jarvis could not read before: Google Keep / Takeout notes (.json), saved web pages and
  notes exports (.html), calendar files (.ics) and repository docs (README/CHANGELOG/.rst/.md). They only read files you already
  placed in an indexed folder; nothing is fetched and no account is contacted.
* NEAR-DUPLICATES - passages or files that say the same thing (word-shingle Jaccard similarity), so answers do not cite the same
  text twice and you can see copies. Reporting only: nothing is deleted.

Limits: similarity is lexical (it catches copies and light edits, not paraphrases); the adapters extract text, not structure.
"""

from __future__ import annotations

import fnmatch
import html
import json
import re
from pathlib import Path
from typing import Any, Iterable

from app.memory.settings_store import SettingsStore
from app.tools.base import ToolError

EXCLUDE_KEY = "retrieval.exclude"
DISABLED_KEY = "retrieval.disabled"
KINDS = ("memory", "document", "conversation")
ADAPTER_EXTENSIONS = {".html", ".htm", ".ics", ".json", ".rst"}
NEAR_DUPLICATE_AT = 0.85


# ------------------------------------------------------------------ switches and exclusions


def exclusions(db: Any) -> list[str]:
    value = SettingsStore(db).get(EXCLUDE_KEY, [])
    return [str(v) for v in value] if isinstance(value, list) else []


def add_exclusion(db: Any, pattern: str) -> list[str]:
    pattern = pattern.strip().replace("\\", "/")
    if not pattern or len(pattern) > 200:
        raise ToolError("Give a folder or name pattern, e.g. 'D:/Private/*' or '*.bak'.", "invalid_pattern")
    now = sorted(set(exclusions(db)) | {pattern})
    if len(now) > 100:
        raise ToolError("At most 100 exclusions.", "too_many")
    SettingsStore(db).set(EXCLUDE_KEY, now)
    return now


def remove_exclusion(db: Any, pattern: str) -> list[str]:
    now = sorted(set(exclusions(db)) - {pattern.strip().replace("\\", "/")})
    SettingsStore(db).set(EXCLUDE_KEY, now)
    return now


def is_excluded(path: str | Path, patterns: Iterable[str]) -> bool:
    """True if the path (any parent folder, the file name, or the whole path) matches an exclusion pattern."""
    text = str(path).replace("\\", "/")
    name = text.rsplit("/", 1)[-1]
    parts = [x for x in text.lower().split("/") if x]
    for raw in patterns:
        p = raw.rstrip("/")
        if "/" not in p and any(fnmatch.fnmatch(part, p.lower()) for part in parts):   # a bare name matches any folder or file with it
            return True
        if fnmatch.fnmatch(text.lower(), p.lower()) or fnmatch.fnmatch(name.lower(), p.lower()) \
                or text.lower().startswith(p.lower().rstrip("*") + "/") or text.lower() == p.lower():
            return True
    return False


def disabled_kinds(db: Any) -> set[str]:
    value = SettingsStore(db).get(DISABLED_KEY, [])
    return {str(v) for v in value if v in KINDS} if isinstance(value, list) else set()


def set_kind(db: Any, kind: str, enabled: bool) -> set[str]:
    if kind not in KINDS:
        raise ToolError(f"Source must be one of: {', '.join(KINDS)}.", "invalid_source")
    now = disabled_kinds(db) - {kind} if enabled else disabled_kinds(db) | {kind}
    SettingsStore(db).set(DISABLED_KEY, sorted(now))
    return now


def purge_excluded(db: Any) -> int:
    """Delete already-indexed passages whose file matches an exclusion. Returns the number of documents removed."""
    patterns = exclusions(db)
    if not patterns:
        return 0
    with db.connect() as conn:
        sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM semantic_items WHERE kind='document'")]
    gone = [s for s in sources if is_excluded(s, patterns)]
    if gone:
        with db.transaction() as conn:
            conn.executemany("DELETE FROM semantic_items WHERE kind='document' AND source=?", [(g,) for g in gone])
    return len(gone)


# ------------------------------------------------------------------ adapters


_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)


def read_html(path: Path) -> str:
    text = html.unescape(_TAGS.sub(" ", path.read_text(encoding="utf-8", errors="ignore")))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def read_ics(path: Path) -> str:
    """Calendar events as sentences: 'Meeting - 2026-11-03 10:00 - notes'."""
    raw = re.sub(r"\r?\n[ \t]", "", path.read_text(encoding="utf-8", errors="ignore"))   # unfold continued lines
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S):
        def field(name: str) -> str:
            m = re.search(rf"^{name}[^:\n]*:(.*)$", block, re.M)
            return (m.group(1).strip().replace("\\n", " ").replace("\\,", ",") if m else "")
        start = field("DTSTART")
        when = f"{start[:4]}-{start[4:6]}-{start[6:8]}" + (f" {start[9:11]}:{start[11:13]}" if len(start) >= 13 else "") if len(start) >= 8 else ""
        line = " - ".join(p for p in (field("SUMMARY"), when, field("LOCATION"), field("DESCRIPTION")) if p)
        if line:
            events.append(line)
    return "\n".join(events)


def read_keep_json(path: Path) -> str:
    """Google Keep / Takeout note files: title + text + list items. Any other JSON is read as plain text only when small."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    if isinstance(data, dict) and ("textContent" in data or "listContent" in data):
        items = [i.get("text", "") for i in data.get("listContent", []) if isinstance(i, dict)]
        return "\n".join(p for p in (data.get("title", ""), data.get("textContent", ""), *items) if p)
    return ""        # arbitrary JSON (configs, tokens) is not indexed


def read_adapter(path: Path) -> str | None:
    """Text for an adapter file, or None when this file is not one the adapters handle."""
    ext = path.suffix.lower()
    if ext in (".html", ".htm"):
        return read_html(path)
    if ext == ".ics":
        return read_ics(path)
    if ext == ".json":
        return read_keep_json(path) or None
    if ext == ".rst":
        return path.read_text(encoding="utf-8", errors="ignore")
    return None


# ------------------------------------------------------------------ near-duplicates


_WORD = re.compile(r"\w+", re.UNICODE)


def shingles(text: str, size: int = 4) -> set[str]:
    words = _WORD.findall(text.lower())
    if len(words) <= size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + size]) for i in range(len(words) - size + 1)}


def similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def near_duplicates(db: Any, threshold: float = NEAR_DUPLICATE_AT, limit: int = 20) -> list[tuple[str, str, float]]:
    """Pairs of indexed documents whose text is nearly the same. Reporting only."""
    with db.connect() as conn:
        rows = conn.execute("SELECT source, text FROM semantic_items WHERE kind='document' ORDER BY source, chunk").fetchall()
    docs: dict[str, list[str]] = {}
    for source, text in rows:
        docs.setdefault(source, []).append(text)
    sets = {s: shingles(" ".join(t)) for s, t in docs.items()}
    names, found = sorted(sets), []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if not sets[a] or not sets[b] or min(len(sets[a]), len(sets[b])) / max(len(sets[a]), len(sets[b])) < threshold * 0.6:
                continue
            score = len(sets[a] & sets[b]) / len(sets[a] | sets[b])
            if score >= threshold:
                found.append((a, b, round(score, 2)))
    return sorted(found, key=lambda r: -r[2])[:limit]
