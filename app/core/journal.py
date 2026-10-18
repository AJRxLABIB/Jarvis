"""Personal journal and voice notes, kept only in the local database.

"Add to my journal: ..." / "Note: ..." (typed or spoken) -> one entry with the time and, if you said one, a mood.
Entries are your own data: they are never sent anywhere and never treated as instructions.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

MAX_TEXT = 8000
MOODS = ("great", "good", "okay", "tired", "stressed", "sad", "angry", "excited", "calm")


def _utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def guess_mood(text: str) -> str:
    low = f" {text.lower()} "
    for mood in MOODS:
        if f" {mood} " in low or f"feeling {mood}" in low or f"feel {mood}" in low:
            return mood
    return ""


class Journal:
    def __init__(self, database) -> None:
        self.db = database

    def add(self, text: str, *, mood: str = "", source: str = "text", when: datetime | None = None) -> int:
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            raise ValueError("Nothing to write down.")
        if source not in ("text", "voice"):
            source = "text"
        mood = (mood or guess_mood(text)).strip().lower()[:20]
        with self.db.transaction() as conn:
            cur = conn.execute("INSERT INTO journal (ts, text, mood, source) VALUES (?, ?, ?, ?)",
                               (_utc(when or datetime.now(timezone.utc)), text, mood, source))
            return int(cur.lastrowid)

    def _rows(self, where: str, params: tuple, limit: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(f"SELECT id, ts, text, mood, source FROM journal {where} ORDER BY ts DESC, id DESC "
                                "LIMIT ?", (*params, max(1, min(limit, 200)))).fetchall()
        out = []
        for r in rows:
            local = datetime.fromisoformat(r["ts"]).astimezone()
            out.append({"id": r["id"], "ts": r["ts"], "when": local.strftime("%a %d %b, %I:%M %p").replace(" 0", " "),
                        "text": r["text"], "mood": r["mood"], "source": r["source"]})
        return out

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._rows("", (), limit)

    def search(self, words: str, limit: int = 20) -> list[dict[str, Any]]:
        terms = [w for w in (words or "").lower().split() if len(w) > 1][:6]
        if not terms:
            return []
        where = "WHERE " + " AND ".join("lower(text) LIKE ? ESCAPE '\\'" for _ in terms)
        escaped = tuple("%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for t in terms)
        return self._rows(where, escaped, limit)

    def on_day(self, day: date, limit: int = 50) -> list[dict[str, Any]]:
        start = datetime.combine(day, time.min).astimezone()
        return self._rows("WHERE ts >= ? AND ts < ?", (_utc(start), _utc(start + timedelta(days=1))), limit)

    def delete(self, entry_id: int) -> bool:
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM journal WHERE id = ?", (entry_id,)).rowcount > 0
