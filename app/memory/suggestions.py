"""Memory suggestions: facts about you noticed in conversation, saved only if you accept them.

When you mention something personal and lasting ("my thesis is due on 15 October", "I work at
FLUX IT", "আমার বাসা ধানমন্ডিতে"), a small local model is asked whether there is a fact worth
remembering. It is offered as a suggestion (Menu -> Memory -> Suggestions); nothing is stored as a
memory until you press Accept. Rejected suggestions are not offered again. Secrets never get in.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Awaitable, Callable

from app.memory.database import Database, utc_now_iso
from app.tools.base import ToolError

MAX_PER_MESSAGE = 3
_PERSONAL = re.compile(r"\b(?:my|mine|i am|i'm|i work|i live|i study|i like|i love|i prefer|i usually|i always|"
                       r"i never|i have|call me|remember that|our)\b|আমার|আমি\s+\S+\s+(?:করি|থাকি|পড়ি|পছন্দ|কাজ)|"
                       r"আমাকে\s+\S+\s+ডাকো", re.I)
_QUESTION_ONLY = re.compile(r"^(?:what|where|when|who|how|why|which|can you|could you|do you|is|are)\b.*\?$", re.I)

PROMPT = ("Read the user's message and list durable facts ABOUT THE USER that would be useful to remember for weeks "
          "or months: their name, work, study, projects and deadlines, family names, places, preferences, routines. "
          "Skip one-off requests, questions, feelings of the moment, and anything secret (passwords, PINs, keys, card "
          "or ID numbers). Write each fact as a short first-person sentence in the user's language, e.g. "
          "\"My thesis deadline is 15 October\". Answer with only a JSON list of strings; [] if there is nothing.\n\n"
          "User's message: {text}")


def worth_checking(text: str) -> bool:
    """Only personal statements go to the model (saves work; most messages are commands)."""
    text = " ".join((text or "").split())
    return 12 <= len(text) <= 600 and bool(_PERSONAL.search(text)) and not _QUESTION_ONLY.match(text)


def parse_facts(answer: str) -> list[str]:
    match = re.search(r"\[.*\]", answer or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    facts = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, str):
            fact = " ".join(item.split()).strip(" .")
            if 6 <= len(fact) <= 200:
                facts.append(fact)
    return facts[:MAX_PER_MESSAGE]


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.8


class SuggestionStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    def add(self, text: str, known: list[str] = ()) -> bool:
        """False if it is a secret, already a memory, or was suggested (or rejected) before."""
        from app.memory.manager import check_not_secret

        try:
            check_not_secret(text)
        except ToolError:
            return False
        if any(_similar(text, k) for k in known):
            return False
        with self.db.connect() as conn:
            seen = [r[0] for r in conn.execute("SELECT text FROM memory_suggestions")]
            if any(_similar(text, s) for s in seen):
                return False
            conn.execute("INSERT INTO memory_suggestions (text, created_at) VALUES (?, ?)", (text, utc_now_iso()))
        return True

    def pending(self) -> list[tuple[int, str]]:
        with self.db.connect() as conn:
            return [(r[0], r[1]) for r in conn.execute(
                "SELECT id, text FROM memory_suggestions WHERE status = 'pending' ORDER BY id")]

    def accept(self, suggestion_id: int, manager) -> str:
        with self.db.connect() as conn:
            row = conn.execute("SELECT text FROM memory_suggestions WHERE id = ? AND status = 'pending'",
                               (suggestion_id,)).fetchone()
        if row is None:
            raise ToolError("That suggestion is gone.", "unknown")
        manager.remember(row[0], provenance="suggestion")
        self._set(suggestion_id, "accepted")
        return row[0]

    def reject(self, suggestion_id: int) -> None:
        self._set(suggestion_id, "rejected")

    def _set(self, suggestion_id: int, status: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE memory_suggestions SET status = ? WHERE id = ?", (status, suggestion_id))


async def suggest(text: str, ask: Callable[[str], Awaitable[str]], store: SuggestionStore, known: list[str]) -> list[str]:
    """Ask the model for facts in ``text`` and store the new ones as pending suggestions."""
    if not worth_checking(text):
        return []
    facts = parse_facts(await ask(PROMPT.format(text=text[:600])))
    return [f for f in facts if store.add(f, known)]
