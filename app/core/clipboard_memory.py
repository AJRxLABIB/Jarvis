"""Clipboard memory: "paste what I copied about the invoice", "what did I copy this morning?".

Off by default (CLIPBOARD_HISTORY=true to switch it on). While on, text you copy is kept locally so it can be searched,
restored and pinned. The rules, all enforced here and tested:

* **Never kept:** anything that looks like a password, key or token (``looks_like_secret``), anything the secret redactor
  would change (API keys, bearer tokens, private keys, passwords in URLs), payment card numbers (Luhn check), very long
  text (over 4000 characters), empty text, and anything copied in Private mode (conversations are not remembered then).
* **Bounded:** at most 200 items and 7 days (CLIPBOARD_RETENTION_DAYS), pinned items excepted; the same text copied again just
  moves to the top instead of piling up.
* **Yours to erase:** the ``clipboard_history`` tool can forget one item, or everything (a confirmed action).
* Text only: no images, files or the source application. Nothing leaves this PC, and restored text goes back to the
  clipboard only when you ask.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.utils.redaction import redact

MAX_CHARS = 4000
MAX_ITEMS = 200
_WORD = re.compile(r"[\w']+", re.UNICODE)


def luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


def has_card_number(text: str) -> bool:
    for m in re.finditer(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn(digits):
            return True
    return False


def refusal(text: str) -> str | None:
    """Why this text must not be kept (None = fine)."""
    from app.tools.productivity import looks_like_secret

    if not text or not text.strip():
        return "empty"
    if len(text) > MAX_CHARS:
        return "too long"
    if looks_like_secret(text):
        return "looks like a password or key"
    if has_card_number(text):
        return "contains a card number"
    if redact(text) != text:
        return "contains something secret-looking"
    return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:24]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ClipboardStore:
    def __init__(self, database, retention_days: int = 7, clock=_now) -> None:
        self._db = database
        self.retention = timedelta(days=max(1, retention_days))
        self._clock = clock

    def add(self, text: str, remember: bool = True) -> str:
        """Keep ``text`` if allowed. Returns "stored", "moved" (already there) or the reason it was refused."""
        if not remember:
            return "private mode"
        why = refusal(text)
        if why:
            return why
        sha, now = _sha(text), self._clock().isoformat(timespec="seconds")
        with self._db.transaction() as conn:
            existing = conn.execute("SELECT id FROM clipboard_items WHERE sha = ?", (sha,)).fetchone()
            if existing:
                conn.execute("UPDATE clipboard_items SET ts = ? WHERE id = ?", (now, existing["id"]))
                return "moved"
            conn.execute("INSERT INTO clipboard_items (ts, text, sha) VALUES (?, ?, ?)", (now, text.strip(), sha))
            self._trim(conn)
        return "stored"

    def _trim(self, conn) -> None:
        cutoff = (self._clock() - self.retention).isoformat(timespec="seconds")
        conn.execute("DELETE FROM clipboard_items WHERE pinned = 0 AND ts < ?", (cutoff,))
        conn.execute("DELETE FROM clipboard_items WHERE pinned = 0 AND id NOT IN "
                     "(SELECT id FROM clipboard_items WHERE pinned = 0 ORDER BY ts DESC, id DESC LIMIT ?)", (MAX_ITEMS,))

    def purge_expired(self) -> None:
        with self._db.transaction() as conn:
            self._trim(conn)

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT id, ts, text, pinned FROM clipboard_items ORDER BY ts DESC, id DESC LIMIT ?",
                                (max(1, min(limit, 50)),)).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Items containing most of the query's words (all of 1-2 words), newest first among equals."""
        words = [w.lower() for w in _WORD.findall(query) if len(w) > 2]
        if not words:
            return self.recent(limit)
        need = len(words) if len(words) <= 2 else max(2, -(-len(words) * 3 // 5))
        scored = []
        for item in self.recent(MAX_ITEMS):
            haystack = item["text"].lower()
            hits = sum(1 for w in words if w in haystack)
            if hits >= need:
                scored.append((hits, item["ts"], item))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [t[2] for t in scored[:limit]]

    def get(self, item_id: int) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT id, ts, text, pinned FROM clipboard_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def pin(self, item_id: int, pinned: bool = True) -> bool:
        with self._db.transaction() as conn:
            return conn.execute("UPDATE clipboard_items SET pinned = ? WHERE id = ?", (int(pinned), item_id)).rowcount > 0

    def forget(self, item_id: int) -> bool:
        with self._db.transaction() as conn:
            return conn.execute("DELETE FROM clipboard_items WHERE id = ?", (item_id,)).rowcount > 0

    def clear(self) -> int:
        with self._db.transaction() as conn:
            return conn.execute("DELETE FROM clipboard_items").rowcount

    def count(self) -> int:
        with self._db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM clipboard_items").fetchone()[0]


class ClipboardWatcher:
    """Feeds changes to the store. ``changed(text)`` is called when the clipboard changes (Qt's dataChanged signal)."""

    def __init__(self, store: ClipboardStore, *, enabled, remember=lambda: True) -> None:
        self.store, self.enabled, self.remember = store, enabled, remember
        self.refused: dict[str, int] = {}          # reason -> count (never the text), for the status line
        self._last = ""

    def changed(self, text: str) -> str:
        if not self.enabled():
            return "off"
        if text == self._last:
            return "same"
        self._last = text
        outcome = self.store.add(text, remember=bool(self.remember()))
        if outcome not in ("stored", "moved"):
            self.refused[outcome] = self.refused.get(outcome, 0) + 1
        return outcome
