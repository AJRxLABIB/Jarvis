"""Learning from real use: what went wrong, a weekly self-review, and answers you liked.

* Every request that failed because something wasn't found ("open my editor" -> no such app),
  that you cancelled, or corrected is noted (the request text only; secrets are redacted).
* The review ("review your mistakes", or once a week by itself) turns them into fixes: "When you
  said 'my editor', did you mean Visual Studio Code?" - say "yes, link it" / "accept fix 1" and
  it becomes a nickname (app/agent/aliases.py).
* "Good answer" saves the last question and answer as an example; similar questions later get it
  as a style example for the model, so answers become more like the ones you liked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from app.memory.database import Database, utc_now_iso
from app.utils.redaction import redact

NOT_FOUND = ("not_found", "unknown", "unknown_page", "unknown_device", "not_running", "no_match", "not_installed")
MAX_EXAMPLES_IN_PROMPT = 2
_STOP = frozenset({"the", "a", "an", "my", "open", "start", "launch", "please", "jarvis", "can", "you", "to", "me", "on",
                   "what", "is", "how", "do", "i", "it", "of", "for", "and", "in", "show"})


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\wঀ-৿]+", (text or "").lower()) if w not in _STOP}


def record(database: Database, kind: str, text: str, detail: str = "") -> None:
    text = redact(" ".join((text or "").split()))[:300]
    if not text:
        return
    with database.connect() as conn:
        conn.execute("INSERT INTO feedback_events (kind, text, detail, created_at) VALUES (?, ?, ?, ?)",
                     (kind, text, redact(detail)[:300], utc_now_iso()))
        conn.execute("DELETE FROM feedback_events WHERE id NOT IN (SELECT id FROM feedback_events ORDER BY id DESC "
                     "LIMIT 500)")


def events(database: Database, days: int = 7) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with database.connect() as conn:
        rows = conn.execute("SELECT kind, text, detail, created_at FROM feedback_events WHERE created_at >= ? "
                            "ORDER BY id", (since,)).fetchall()
    return [dict(r) for r in rows]


@dataclass(frozen=True)
class Fix:
    heard: str  # what you said (the name part)
    kind: str  # app | site | page
    target: str  # what it should open
    times: int

    def describe(self) -> str:
        return f"When you said '{self.heard}' ({self.times}x), did you mean {self.target}?"


_NAME = re.compile(r"^(?:please\s+)?(?:open|start|launch|run|show|go to|close|switch to)?\s*(?:the\s+|my\s+)?(?P<name>.+?)"
                   r"(?:\s+(?:app|application|website|site|settings))?[.!?]*$", re.I)


def review(database: Database, candidates: dict[str, str], days: int = 7, known: set[str] = frozenset()) -> list[Fix]:
    """Fixes for names that weren't found: the closest-sounding app / site / page from ``candidates``
    ({name: kind}). Names that are already nicknames (``known``) are skipped."""
    from app.utils.phonetic import similarity

    counts: dict[str, int] = {}
    for event in events(database, days):
        if event["kind"] != "not_found":
            continue
        match = _NAME.match(event["text"].strip())
        name = (match.group("name") if match else event["text"]).strip().lower()
        if 2 <= len(name) <= 40 and name not in known:
            counts[name] = counts.get(name, 0) + 1
    fixes = []
    for name, times in sorted(counts.items(), key=lambda kv: -kv[1]):
        scored = sorted(((max(similarity(name, c), SequenceMatcher(None, name, c.lower()).ratio()), c)
                         for c in candidates), reverse=True)
        if scored and scored[0][0] >= 0.5:
            target = scored[0][1]
            fixes.append(Fix(name, candidates[target], target, times))
    return fixes[:8]


def summary(database: Database, days: int = 7) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events(database, days):
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    return counts


# ------------------------------------------------------------------ answers you liked

def save_example(database: Database, question: str, answer: str) -> None:
    question, answer = redact(question.strip())[:500], redact(answer.strip())[:1500]
    if not question or not answer:
        return
    with database.connect() as conn:
        conn.execute("INSERT INTO answer_examples (question, answer, created_at) VALUES (?, ?, ?)",
                     (question, answer, utc_now_iso()))
        conn.execute("DELETE FROM answer_examples WHERE id NOT IN (SELECT id FROM answer_examples ORDER BY id DESC "
                     "LIMIT 200)")


def similar_examples(database: Database, question: str, limit: int = MAX_EXAMPLES_IN_PROMPT) -> list[tuple[str, str]]:
    wanted = _words(question)
    if not wanted:
        return []
    with database.connect() as conn:
        rows = conn.execute("SELECT question, answer FROM answer_examples ORDER BY id DESC LIMIT 200").fetchall()
    scored = []
    for q, a in rows:
        overlap = len(wanted & _words(q)) / max(1, len(wanted | _words(q)))
        if overlap >= 0.3:
            scored.append((overlap, q, a))
    scored.sort(reverse=True)
    return [(q, a) for _, q, a in scored[:limit]]


def examples_prompt(examples: list[tuple[str, str]]) -> str:
    if not examples:
        return ""
    lines = ["Answers the user liked before (match their style and length, not their facts):"]
    for question, answer in examples:
        lines.append(f"- User: {question[:200]}\n  You: {answer[:400]}")
    return "\n".join(lines)
