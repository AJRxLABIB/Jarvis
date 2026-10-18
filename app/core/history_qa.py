"""Ask your own history: "when did I last run the backup?", "how many times did I open Spotify this week?".

Answers come only from what Jarvis recorded: its audit trail of tool calls (rebuilt as the activity timeline) and the to-dos
you finished. Nothing is guessed and nothing new is collected. Every answer names the records it is based on (audit ids) and
says so when it cannot know: apps you opened by hand, things said before the log existed, or entries you deleted are not there.

Matching is deliberately simple and explainable: your words are reduced to stems ("opened" -> "open", "backups" -> "backup"),
filler words are dropped, and a record matches when all of your one or two key words (about 60 % of more) appear - everyday verbs such as "run" or
"open" only count when there is nothing else to match on - in its tool name, outcome,
the request that caused it, or its arguments. The answer shows which record matched, so a wrong match is visible.
"""

from __future__ import annotations

from app.utils.timefmt import time12 as _clock

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core import timeline

_STOP = frozenset("""a an the my me i you we it of to for in on at by with and or did do does when was were is are how many
times time last first ever again this that these those today yesterday week month day days ago since up""".split())
_IRREGULAR = {"ran": "run", "sent": "send", "made": "make", "wrote": "write", "began": "begin", "did": "do", "went": "go",
              "took": "take", "saw": "see", "got": "get", "bought": "buy", "found": "find", "backed": "backup", "back": "backup"}
# Everyday verbs that rarely appear in a record the way you say them ("run the backup" vs the tool "create_backup").
# They only count when there is nothing else to match on; the nouns decide.
_GENERIC = frozenset({"run", "open", "start", "launch", "finish", "complete", "do", "make", "use", "create", "close", "play",
                      "send", "check", "take", "get", "set", "turn"})
_WORD = re.compile(r"[\w']+", re.UNICODE)
MAX_SCAN = 2000


def stem(word: str) -> str:
    word = word.lower().strip("'")
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 5 and word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def keywords(question: str) -> list[str]:
    seen: list[str] = []
    for w in _WORD.findall(question.lower()):
        if w in _STOP:
            continue
        s = stem(w)
        if s and s not in _STOP and s not in seen:
            seen.append(s)
    return seen


@dataclass(frozen=True)
class Match:
    at: datetime              # local time
    what: str                 # a sentence about the record
    source: str               # where it came from, e.g. "audit #123"
    ok: bool
    score: float


def _haystack_words(*parts: Any) -> set[str]:
    words: set[str] = set()
    for part in parts:
        text = json.dumps(part, ensure_ascii=False) if isinstance(part, (dict, list)) else str(part or "")
        words |= {stem(w) for w in _WORD.findall(text.replace("_", " ").lower())}
    return words


def search(database, question: str, now: datetime, since: datetime | None = None, until: datetime | None = None,
           include_failed: bool = False) -> tuple[list[Match], list[str]]:
    """Matching records, newest first, and the keywords used. A record matches when all of 1-2 keywords, or about 60 % of
    more, appear in its tool name, outcome, request or arguments."""
    words = keywords(question)
    if not words:
        return [], words
    nouns = [w for w in words if w not in _GENERIC] or words
    need = len(nouns) if len(nouns) <= 2 else max(2, -(-len(nouns) * 3 // 5))     # all of 1-2 words, else about 60 %
    matches: list[Match] = []
    for entry in timeline.entries(database, since=since, until=until, limit=MAX_SCAN):
        if not entry["ok"] and not include_failed:
            continue
        at = datetime.fromisoformat(entry["time"]).astimezone(now.tzinfo)
        have = _haystack_words(entry["tool"], entry["outcome"], entry["request"], entry.get("arguments") or {})
        hits = sum(1 for w in nouns if w in have)
        if hits < need:
            continue
        hits = sum(1 for w in words if w in have)                  # the score still counts the verbs
        matches.append(Match(at, f"{entry['tool'].replace('_', ' ')}: {entry['outcome'][:90] or 'done'}", f"audit #{entry['id']}",
                             entry["ok"], hits / len(words)))
    matches += _finished_todos(database, words, nouns, need, now, since, until)
    matches.sort(key=lambda m: m.at, reverse=True)
    return matches, words


def _finished_todos(database, words: list[str], nouns: list[str], need: int, now: datetime, since, until) -> list[Match]:
    out = []
    with database.connect() as conn:
        rows = conn.execute("SELECT id, text, done_at FROM todos WHERE done = 1 AND done_at IS NOT NULL").fetchall()
    for row in rows:
        try:
            at = datetime.fromisoformat(row["done_at"]).astimezone(now.tzinfo)
        except ValueError:
            continue
        if (since and at < since) or (until and at >= until):
            continue
        have = _haystack_words(row["text"])
        if sum(1 for w in nouns if w in have) >= need:
            hits = sum(1 for w in words if w in have)
            out.append(Match(at, f"finished the to-do \"{row['text'][:80]}\"", f"to-do #{row['id']}", True, hits / len(words)))
    return out


def when_text(at: datetime, now: datetime) -> str:
    days = (now.date() - at.date()).days
    clock = _clock(at)
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"yesterday at {clock}"
    if days < 7:
        return f"{at:%A} at {clock} ({days} days ago)"
    return f"{at:%A %d %B} at {clock} ({days} days ago)"


CAVEAT = "This is only what Jarvis recorded; things you did by hand are not in it."


def answer_last(database, question: str, now: datetime, since=None, until=None) -> tuple[str, list[Match]]:
    found, words = search(database, question, now, since, until)
    if not words:
        return "Tell me what to look for, for example 'when did I last run the backup'.", []
    if not found:
        return f"I can't find anything about \"{' '.join(words)}\" in what Jarvis recorded. {CAVEAT}", []
    top = found[0]
    return (f"The last time was {when_text(top.at, now)} - {top.what} ({top.source}). {CAVEAT}"), found[:5]


def answer_count(database, question: str, now: datetime, since: datetime, until: datetime | None, label: str) -> tuple[str, list[Match]]:
    found, words = search(database, question, now, since, until)
    if not words:
        return "Tell me what to count, for example 'how many times did I open Spotify this week'.", []
    if not found:
        return f"I find 0 records about \"{' '.join(words)}\" {label}. {CAVEAT}", []
    days = len({m.at.date() for m in found})
    return (f"{len(found)} record{'s' if len(found) != 1 else ''} about \"{' '.join(words)}\" {label}, on {days} "
            f"day{'s' if days != 1 else ''}; the latest {when_text(found[0].at, now)}. {CAVEAT}"), found[:5]


def week_start(now: datetime) -> datetime:
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)
