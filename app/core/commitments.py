"""A ledger of promises: what YOU said you would do, and what you are WAITING for from someone else - found in your mail and messages, kept until settled.

Why: an inbox shows what arrived, not what is still owed. "I'll send it by Friday" in a sent mail and "could you share the invoice?" with no answer are both
easy to forget; this finds them, gives them a due date when one was said, and shows the overdue ones first.

How (deliberately rule-based, so every item can show the sentence it came from):
* ``i_owe``  - in text YOU wrote: "I will / I'll / let me / I am going to + verb ..." (also a few Bangla forms); in text someone sent YOU: a direct request
  ("please / could you / can you / kindly ... ").
* ``they_owe`` - in text YOU wrote: a request to the other person ("could you ...", "please send ...", "let me know ...", a direct question about a deliverable).
* Pleasantries are dropped ("I'll be there", "I will let you know" is kept; "I hope", "I think", "I'll be happy/glad" are not), as are newsletters and no-reply senders.
* A due date is taken only from words in that sentence ("by Friday", "tomorrow", "before 5 pm", "2026-11-20"); nothing is invented - no date said, no due date.
* Settling: a later message from the person who owed it, in the same conversation, marks the item ``answered`` - a SUGGESTION that it may be done, shown
  separately; only you (or an explicit "done") close it. Dismissed items never come back (the same sentence from the same message has the same id).

It will miss promises phrased unusually and will sometimes keep a polite phrase that was not a promise; that is why each item quotes its source and can be
dismissed. Storage: ``commitments`` (migration 43). The text kept is the one sentence, capped at 200 characters - not the message.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

KINDS = ("i_owe", "they_owe")
MAX_TEXT = 200
_SENTENCE = re.compile(r"(?<=[.!?।\n])\s+|\n+")
_PROMISE = re.compile(r"\b(?:i\s*(?:will|'ll|’ll|shall)|i\s*am going to|i'm going to|i’m going to|let me|i\s*(?:can|could) (?:send|share|do|check|get|prepare|finish|fix)|"
                      r"i'll|we\s*(?:will|'ll|’ll))\s+(?!be (?:happy|glad|honest|there for)|never|always|miss|not\b)([a-z][^.!?\n]{3,160})", re.I)
_PROMISE_BN = re.compile(r"([^।.!?\n]{3,120}?(?:পাঠাবো|পাঠাব|পাঠিয়ে দিব|পাঠিয়ে দেব|দিয়ে দিব|দিয়ে দেব|করে দিব|করে দেব|জানাবো|জানাব|দেখবো|দেখব))")
_REQUEST = re.compile(r"\b(?:please|kindly|pls|plz)\s+(?!find|note|see attached|be (?:informed|advised)|do not reply|don't reply|ignore)([a-z][^.!?\n]{3,160})|"
                      r"\b(?:could|can|would|will) you(?: please| kindly)?\s+([a-z][^.!?\n]{3,160})|\b(?:let me know|send me|share with me|get back to me)\b([^.!?\n]{0,160})|"
                      r"\b(?:need you to|i need|we need)\s+([a-z][^.!?\n]{3,160})", re.I)
_REQUEST_BN = re.compile(r"([^।.!?\n]{3,120}?(?:পাঠিয়ে দিন|পাঠিয়ে দিও|পাঠান|পাঠাও|জানাবেন|জানাও|দিয়ে দিন|করে দিন|করে দিও))")
_SOFT = re.compile(r"\b(?:i hope|i think|i guess|i believe|i wish|if you (?:want|like|need)|feel free|no need|don't worry|thank(?:s| you)|congrat|happy birthday)\b", re.I)
_BULK = re.compile(r"no-?reply|do-?not-?reply|newsletter|notification|mailer-daemon|unsubscribe|noreply", re.I)
_DUE = re.compile(r"\b(?:by|before|until|on|due|this|next)?\s*(today|tonight|tomorrow|(?:mon|tues|wednes|thurs|fri|satur|sun)day|\d{4}-\d{2}-\d{2}|"
                  r"end of (?:the )?(?:day|week|month)|eod|eow|next week)\b|(আজ|আজকে|কাল|আগামীকাল)", re.I)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class Found:
    kind: str            # i_owe | they_owe
    who: str             # the other person
    text: str            # the sentence it came from (capped)
    due: str             # ISO date or ""
    source: str          # mail | whatsapp | sms | chat
    ref: str             # the message it came from (id or hash)
    thread: str          # conversation key (the other person, lower-cased)
    created: float

    @property
    def id(self) -> str:
        return hashlib.sha256(f"{self.kind}|{self.thread}|{self.ref}|{self.text.lower()}".encode()).hexdigest()[:16]


def due_date(sentence: str, now: datetime) -> str:
    """The date a sentence names ("by Friday" -> the coming Friday). "" when it names none."""
    m = _DUE.search(sentence)
    if not m:
        return ""
    word = (m.group(1) or m.group(2) or "").lower()
    today = now.date()
    if word in ("today", "tonight", "eod", "end of day", "end of the day", "আজ", "আজকে"):
        return today.isoformat()
    if word in ("tomorrow", "কাল", "আগামীকাল"):
        return (today + timedelta(days=1)).isoformat()
    if word in _WEEKDAYS:
        return (today + timedelta(days=(_WEEKDAYS.index(word) - today.weekday()) % 7 or 7)).isoformat()
    if word in ("eow", "end of week", "end of the week"):
        return (today + timedelta(days=(4 - today.weekday()) % 7)).isoformat()
    if word == "next week":
        return (today + timedelta(days=7 - today.weekday())).isoformat()
    if word in ("end of month", "end of the month"):
        first_next = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        return (first_next - timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(word).isoformat()
    except ValueError:
        return ""


def _clip(sentence: str) -> str:
    return " ".join(sentence.split())[:MAX_TEXT]


def extract(text: str, *, mine: bool, who: str, source: str, ref: str, when: float, now: datetime | None = None) -> list[Found]:
    """Promises and requests in one message. ``mine`` = you wrote it. ``who`` = the other person."""
    now = now or datetime.now()
    if not text or not who.strip() or _BULK.search(who):
        return []
    out: list[Found] = []
    seen: set[str] = set()
    thread = " ".join(who.lower().split())
    for sentence in _SENTENCE.split(text[:6000]):
        s = sentence.strip()
        if len(s) < 8 or len(s) > 400 or s.startswith(">") or _SOFT.search(s):
            continue
        kinds = []
        if mine:
            if _PROMISE.search(s) or _PROMISE_BN.search(s):
                kinds.append("i_owe")
            if _REQUEST.search(s) or _REQUEST_BN.search(s) or (s.endswith("?") and re.search(r"\b(?:when|did you|have you|any update|status of)\b", s, re.I)):
                kinds.append("they_owe")
        elif _REQUEST.search(s) or _REQUEST_BN.search(s):
            kinds.append("i_owe")
        for kind in kinds[:1]:                                    # one item per sentence: a promise wins over a request in the same sentence
            item = Found(kind, who.strip()[:80], _clip(s), due_date(s, now), source, ref, thread, when)
            if item.id not in seen:
                seen.add(item.id)
                out.append(item)
        if len(out) >= 5:                                         # a long message is not a to-do list: the first five are enough
            break
    return out


# ---------------------------------------------------------------- the ledger

class Ledger:
    def __init__(self, db: Any) -> None:
        self.db = db

    def add(self, items: Iterable[Found]) -> int:
        """Stores new items; an id already known (open, done or dismissed) is left exactly as it is. -> how many were new."""
        new = 0
        with self.db.transaction() as c:
            for it in items:
                cur = c.execute("INSERT OR IGNORE INTO commitments (id, kind, who, text, due, source, ref, thread, created, status) VALUES (?,?,?,?,?,?,?,?,?, 'open')",
                                (it.id, it.kind, it.who, it.text, it.due, it.source, it.ref, it.thread, it.created))
                new += cur.rowcount
        return new

    def settle_from(self, messages: Iterable[dict[str, Any]]) -> int:
        """A later message from the person who owed something marks that item 'answered' (a hint, not a closure). ``messages``: {who, mine, when}."""
        changed = 0
        with self.db.transaction() as c:
            for m in messages:
                thread = " ".join(str(m.get("who", "")).lower().split())
                if not thread:
                    continue
                kind = "i_owe" if m.get("mine") else "they_owe"       # I wrote again -> my promise may be kept; they wrote again -> they may have answered
                changed += c.execute("UPDATE commitments SET status='answered', settled=? WHERE status='open' AND kind=? AND thread=? AND created < ?",
                                     (float(m.get("when") or 0), kind, thread, float(m.get("when") or 0))).rowcount
        return changed

    def set_status(self, item_id: str, status: str) -> bool:
        if status not in ("open", "done", "dismissed"):
            raise ValueError("status must be open, done or dismissed")
        with self.db.transaction() as c:
            rows = c.execute("SELECT id FROM commitments WHERE id LIKE ?", (item_id.strip()[:16] + "%",)).fetchall()
            if len(rows) != 1:
                return False
            c.execute("UPDATE commitments SET status=? WHERE id=?", (status, rows[0]["id"]))
        return True

    def items(self, status: tuple[str, ...] = ("open", "answered"), kind: str | None = None) -> list[dict[str, Any]]:
        q = f"SELECT * FROM commitments WHERE status IN ({','.join('?' * len(status))})" + (" AND kind = ?" if kind else "") + " ORDER BY CASE WHEN due = '' THEN 1 ELSE 0 END, due, created DESC"
        with self.db.connect() as c:
            return [dict(r) for r in c.execute(q, (*status, *([kind] if kind else [])))]

    def prune(self, keep_days: int = 180, now: float | None = None) -> int:
        import time

        with self.db.transaction() as c:
            return c.execute("DELETE FROM commitments WHERE status IN ('done','dismissed') AND created < ?", ((now or time.time()) - keep_days * 86400,)).rowcount


def bucket(item: dict[str, Any], today: date) -> str:
    """overdue | today | soon (next 3 days) | later | undated | answered"""
    if item["status"] == "answered":
        return "answered"
    if not item["due"]:
        return "undated"
    try:
        d = date.fromisoformat(item["due"])
    except ValueError:
        return "undated"
    return "overdue" if d < today else "today" if d == today else "soon" if (d - today).days <= 3 else "later"


def describe(items: list[dict[str, Any]], today: date | None = None, limit: int = 12) -> str:
    today = today or date.today()
    if not items:
        return "Nothing is open: no promises you made and nothing you are waiting for were found. (Run a scan after new mail or messages arrive.)"
    order = ("overdue", "today", "soon", "later", "undated", "answered")
    label = {"overdue": "OVERDUE", "today": "due today", "soon": "due soon", "later": "later", "undated": "no date said", "answered": "probably settled (they/you wrote again) - confirm"}
    lines = []
    for kind, title in (("i_owe", "You owe"), ("they_owe", "You are waiting for")):
        mine = sorted((i for i in items if i["kind"] == kind), key=lambda i: order.index(bucket(i, today)))
        if not mine:
            continue
        lines.append(f"{title} ({len(mine)}):")
        for i in mine[:limit]:
            b = bucket(i, today)
            lines.append(f"  [{i['id'][:6]}] {i['who']}: \"{i['text'][:110]}\" - {label[b]}" + (f" ({i['due']})" if i["due"] and b != "answered" else "") + f" [{i['source']}]")
        if len(mine) > limit:
            lines.append(f"  ... and {len(mine) - limit} more")
    lines.append("Found by wording, so check the quote; say 'done <id>' or 'dismiss <id>'.")
    return "\n".join(lines)
