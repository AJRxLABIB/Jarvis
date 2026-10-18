"""Adaptive learning: questions you want to remember, spaced repetition, and mastery per topic.

Each item is a question and its answer with where it came from: ``you`` (you wrote it), ``document: name`` (taken from
your material) or ``ai: ...`` (written by the model - shown as such, because an AI-written answer can be wrong). Reviews
use the SM-2 spaced-repetition rule: a good answer pushes the next review further away, a bad one brings it back to
tomorrow. Mastery of a topic is the share of answers you got right, never an invented score. Everything is kept in your
database, can be listed, and a topic can be deleted completely.

Answers are checked by word overlap (a deterministic check, no model): good for facts and terms, not for essays - so
you can always say how well you really knew it (quality 0-5) and that wins.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.tools.base import ToolError

MIN_EASE = 1.3
_WORD = re.compile(r"[\w]+", re.UNICODE)
_STOP = frozenset({"the", "a", "an", "of", "to", "is", "are", "and", "or", "in", "on", "for", "it", "that", "this", "with"})


def schedule(ease: float, interval: int, repetitions: int, quality: int) -> tuple[float, int, int]:
    """One SM-2 step -> (new ease, new interval in days, new repetitions)."""
    if not 0 <= quality <= 5:
        raise ToolError("Quality is 0 (no idea) to 5 (perfect).", "invalid_quality")
    if quality < 3:
        return max(MIN_EASE, ease - 0.2), 1, 0
    repetitions += 1
    interval = 1 if repetitions == 1 else 6 if repetitions == 2 else max(1, round(interval * ease))
    ease = max(MIN_EASE, ease + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    return round(ease, 2), interval, repetitions


def overlap_quality(expected: str, given: str) -> int:
    """0-5 from how many of the expected answer's key words were given. Words only; numbers must match exactly."""
    want = [w for w in _WORD.findall(expected.lower()) if w not in _STOP]
    got = set(_WORD.findall(given.lower()))
    if not want:
        return 3 if given.strip() else 0
    share = sum(1 for w in want if w in got) / len(want)
    return 5 if share >= 0.95 else 4 if share >= 0.8 else 3 if share >= 0.6 else 2 if share >= 0.3 else 0 if share == 0 else 1


class LearningStore:
    def __init__(self, database, today=lambda: date.today()) -> None:
        self._db, self._today = database, today

    def add(self, topic: str, question: str, answer: str, source: str = "you", language: str = "en") -> int:
        topic = " ".join(topic.lower().split())[:60]
        if not topic or not question.strip() or not answer.strip():
            raise ToolError("A learning item needs a topic, a question and an answer.", "invalid_item")
        if not (source == "you" or source.startswith(("document:", "ai:"))):
            raise ToolError("The source must be 'you', 'document: <name>' or 'ai: <note>'.", "invalid_source")
        with self._db.transaction() as conn:
            dup = conn.execute("SELECT id FROM learning_items WHERE topic = ? AND lower(question) = lower(?)",
                               (topic, question.strip())).fetchone()
            if dup:
                return int(dup["id"])
            cursor = conn.execute(
                "INSERT INTO learning_items (topic, question, answer, source, language, due, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (topic, question.strip()[:400], answer.strip()[:800], source[:80],
                                                  language[:8], self._today().isoformat(), self._today().isoformat()))
            return int(cursor.lastrowid)

    def due(self, topic: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        query = "SELECT id, topic, question, source, interval_days FROM learning_items WHERE due <= ?"
        params: list[Any] = [self._today().isoformat()]
        if topic:
            query += " AND topic = ?"
            params.append(" ".join(topic.lower().split()))
        with self._db.connect() as conn:
            return [dict(r) for r in conn.execute(query + " ORDER BY due, id LIMIT ?", (*params, limit))]

    def item(self, item_id: int) -> dict[str, Any]:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM learning_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ToolError("There is no such learning item.", "not_found")
        return dict(row)

    def review(self, item_id: int, given: str = "", quality: int | None = None) -> dict[str, Any]:
        item = self.item(item_id)
        auto = quality is None
        q = overlap_quality(item["answer"], given) if auto else quality
        ease, interval, reps = schedule(item["ease"], item["interval_days"], item["repetitions"], q)
        due = (self._today() + timedelta(days=interval)).isoformat()
        right = q >= 3
        with self._db.transaction() as conn:
            conn.execute("UPDATE learning_items SET ease = ?, interval_days = ?, repetitions = ?, due = ?, "
                         "correct = correct + ?, wrong = wrong + ? WHERE id = ?",
                         (ease, interval, reps, due, int(right), int(not right), item_id))
        return {"correct": right, "quality": q, "graded_by": "word overlap" if auto else "you", "next_review": due,
                "next_in_days": interval, "answer": item["answer"], "source": item["source"],
                "explain": None if right else f"The answer was: {item['answer']} (source: {self._label(item['source'])})."}

    @staticmethod
    def _label(source: str) -> str:
        if source.startswith("ai:"):
            return "written by the AI - check it against your material"
        if source.startswith("document:"):
            return source.split(":", 1)[1].strip() or "your document"
        return "you"

    def mastery(self) -> list[dict[str, Any]]:
        today = self._today().isoformat()
        with self._db.connect() as conn:
            rows = conn.execute("SELECT topic, COUNT(*) AS items, SUM(correct) AS correct, SUM(wrong) AS wrong, "
                                "SUM(CASE WHEN due <= ? THEN 1 ELSE 0 END) AS due_now, "
                                "SUM(CASE WHEN repetitions >= 3 THEN 1 ELSE 0 END) AS learned "
                                "FROM learning_items GROUP BY topic ORDER BY topic", (today,)).fetchall()
        out = []
        for r in rows:
            tries = (r["correct"] or 0) + (r["wrong"] or 0)
            out.append({"topic": r["topic"], "items": r["items"], "due_now": r["due_now"], "learned": r["learned"],
                        "answered": tries, "correct_rate": round(100 * (r["correct"] or 0) / tries) if tries else None})
        return out

    def forget_topic(self, topic: str) -> int:
        with self._db.transaction() as conn:
            return conn.execute("DELETE FROM learning_items WHERE topic = ?", (" ".join(topic.lower().split()),)).rowcount

    def describe(self) -> str:
        rows = self.mastery()
        if not rows:
            return "Nothing is being learned yet. Add questions, or ask Jarvis to make some from your notes."
        parts = []
        for r in rows:
            rate = f"{r['correct_rate']}% right" if r["correct_rate"] is not None else "not answered yet"
            parts.append(f"{r['topic']}: {r['items']} item(s), {r['learned']} learned, {r['due_now']} due, {rate}")
        return "; ".join(parts) + "."
