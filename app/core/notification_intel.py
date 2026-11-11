"""Notification intelligence: what a phone notification IS and how much it matters, decided by plain rules (no model, nothing leaves the PC).

Labels: critical, important, action_required, routine, promotional, suspicious. Also: duplicate detection (same app + same wording, digits ignored),
deadline phrases ("today", "by 5 pm", "due 12 Nov", "kal", "আজ"), and a short digest. English, Bangla and Banglish cues.

Honest limits: rules read the words, not the sender's identity - a phishing text that avoids the cue words is "routine"; a real bank alert that
trips the phishing cues (a link + urgency) is "suspicious" and says WHY so you can judge. It never deletes, replies or opens links.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

CRITICAL, IMPORTANT, ACTION, ROUTINE, PROMO, SUSPICIOUS = "critical", "important", "action_required", "routine", "promotional", "suspicious"
LABELS = (CRITICAL, SUSPICIOUS, ACTION, IMPORTANT, ROUTINE, PROMO)             # display order = attention order
PRIORITY = {CRITICAL: "urgent", SUSPICIOUS: "high", ACTION: "high", IMPORTANT: "normal", ROUTINE: "low", PROMO: "low"}

_P = lambda words: re.compile(r"(?:" + "|".join(words) + r")", re.I | re.U)  # noqa: E731

_SHORT_LINK = _P([r"\bbit\.ly/", r"\btinyurl\.com/", r"\bt\.co/", r"\bis\.gd/", r"\bcutt\.ly/", r"\brb\.gy/", r"\bgoo\.gl/"])
_URL = re.compile(r"https?://\S+|\bwww\.\S+", re.I)
_PHISH_WORDS = _P([r"verify your (?:account|identity|card)", r"account (?:is |has been )?(?:suspended|locked|blocked|limited)", r"click (?:here|the link|below)",
                   r"confirm your (?:password|pin|details)", r"you(?:'ve| have)? won", r"winner", r"claim your", r"lottery", r"free gift", r"final warning",
                   r"update your (?:bank|payment) (?:details|info)", r"share (?:the |your )?(?:otp|pin|code)", r"অ্যাকাউন্ট.{0,12}(?:বন্ধ|ব্লক)", r"লিংকে ক্লিক",
                   r"account (?:bondho|block)", r"link e click"])
_URGENT = _P([r"\burgent(?:ly)?\b", r"\bimmediately\b", r"within \d+ ?(?:hours?|minutes?|hrs?)", r"\bact now\b", r"\blast chance\b", r"এখনই", r"\bshigroi\b"])
_CRITICAL = _P([r"unauthori[sz]ed", r"\bfraud", r"security alert", r"new (?:sign[- ]?in|login) (?:attempt|from)", r"suspicious (?:activity|login|sign[- ]?in)",
                r"\bpayment (?:failed|declined)", r"\bcard (?:declined|blocked)", r"password (?:was )?changed", r"low battery|battery (?:critical|very low)", r"\bsos\b",
                r"emergency", r"smoke|fire alarm", r"সন্দেহজনক", r"জরুরি"])
_ACTION = _P([r"please (?:confirm|approve|reply|respond|review|sign|submit|pay)", r"action (?:needed|required)", r"\brsvp\b", r"\bdue\b", r"\bdeadline\b",
              r"\bexpires?\b", r"\binvoice\b", r"\bpayment (?:due|pending|reminder)", r"reply (?:needed|with)", r"আবেদন|জমা দিতে|পরিশোধ", r"jama dite hobe|payment korte hobe|reply dao"])
_PROMO = _P([r"\d{1,3}% ?off", r"\bsale\b", r"\boffers?\b", r"\bdiscounts?\b", r"\bcoupon", r"\bdeals?\b", r"\bcashback\b", r"limited[- ]time", r"unsubscribe",
             r"\bpromo", r"buy \d+ get", r"flash sale", r"অফার|ছাড়"])
_IMPORTANT = _P([r"\bmeeting\b", r"\bcalendar\b", r"\bdelivered\b", r"out for delivery", r"arriving (?:today|tomorrow)", r"\bflight\b", r"\bboarding\b",
                 r"\bappointment\b", r"\bmissed call\b", r"\binterview\b", r"\bresults?\b", r"\bclass (?:at|starts)\b", r"মিটিং|পরীক্ষা"])
_PERSON_APPS = _P([r"whatsapp", r"messenger", r"telegram", r"signal", r"messages", r"\bsms\b", r"imo", r"viber", r"gmail", r"outlook", r"mail"])
_TAG = re.compile(r"^\[(?:CRITICAL|Suspicious|Action required|Important|Promotional)\] ")
_OTP = re.compile(r"\b\d{4,8}\b")

_DEADLINE = re.compile(
    r"\b(today|tonight|tomorrow|this (?:evening|afternoon|morning)|by (?:\d{1,2}(?::\d{2})? ?(?:am|pm)?)|due (?:on |by )?(?:[a-z]{3,9} \d{1,2}|\d{1,2} [a-z]{3,9}|\d{4}-\d{2}-\d{2}|tomorrow|today)|"
    r"\d{4}-\d{2}-\d{2}|kal|aj(?:ke)?|আজ|কাল)\b", re.I | re.U)


@dataclass
class Result:
    label: str
    priority: str
    reasons: list[str] = field(default_factory=list)
    deadline: str = ""                        # the phrase found ("by 5 pm"), not a guess at a date
    duplicate: bool = False
    app: str = ""
    title: str = ""

    def tag(self) -> str:
        tag = {CRITICAL: "CRITICAL", SUSPICIOUS: "Suspicious", ACTION: "Action required", IMPORTANT: "Important", ROUTINE: "", PROMO: "Promotional"}[self.label]
        return f"[{tag}] " if tag else ""

    def headline(self) -> str:
        return self.tag() + self.title

    def explain(self) -> str:
        why = "; ".join(self.reasons) or "nothing special in the wording"
        return f"{self.label.replace('_', ' ')} - {why}" + (f" - deadline mentioned: {self.deadline}" if self.deadline else "")


def _signature(app: str, title: str, text: str) -> str:
    norm = re.sub(r"\d+", "#", f"{app}|{title}|{text}".lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]


class Classifier:
    """Stateful only for duplicate detection (the last ``window`` signatures)."""

    def __init__(self, window: int = 200) -> None:
        self._seen: list[str] = []
        self.window = window

    def classify(self, app: str, title: str, text: str) -> Result:
        body = f"{title}\n{text}"
        reasons: list[str] = []
        sig = _signature(app, title, text)
        duplicate = sig in self._seen
        self._seen.append(sig)
        del self._seen[:-self.window]
        deadline = (_DEADLINE.search(body).group(1) if _DEADLINE.search(body) else "")

        phish = [name for name, rx in (("asks you to verify / click / share a code", _PHISH_WORDS), ("a shortened link", _SHORT_LINK)) if rx.search(body)]
        has_url = bool(_URL.search(body))
        urgent = bool(_URGENT.search(body))
        if phish and (has_url or urgent or len(phish) > 1 or _PHISH_WORDS.search(body)):
            reasons = phish + (["urgent wording"] if urgent else []) + (["contains a link"] if has_url else [])
            return Result(SUSPICIOUS, PRIORITY[SUSPICIOUS], reasons, deadline, duplicate, app, title)
        if urgent and has_url and not _CRITICAL.search(body) and _PROMO.search(body) is None and not _PERSON_APPS.search(app):
            return Result(SUSPICIOUS, PRIORITY[SUSPICIOUS], ["urgent wording with a link from an app that is not a person"], deadline, duplicate, app, title)
        hit = _CRITICAL.search(body)
        if hit:
            return Result(CRITICAL, PRIORITY[CRITICAL], [f"mentions '{hit.group(0).strip()}'"], deadline, duplicate, app, title)
        hit = _ACTION.search(body)
        if hit:
            return Result(ACTION, PRIORITY[ACTION], [f"mentions '{hit.group(0).strip()}'"], deadline, duplicate, app, title)
        if _PROMO.search(body):
            return Result(PROMO, PRIORITY[PROMO], ["offer / sale wording"], deadline, duplicate, app, title)
        hit = _IMPORTANT.search(body)
        if hit:
            return Result(IMPORTANT, PRIORITY[IMPORTANT], [f"mentions '{hit.group(0).strip()}'"], deadline, duplicate, app, title)
        if _PERSON_APPS.search(app) and (title or text):
            return Result(IMPORTANT if title and not _OTP.fullmatch((text or "").strip()) else ROUTINE, "normal", ["a message app"], deadline, duplicate, app, title)
        return Result(ROUTINE, PRIORITY[ROUTINE], reasons, deadline, duplicate, app, title)


def reminder_hint(result: Result, now: datetime | None = None) -> str:
    """A suggestion (never created automatically) when a notification names a deadline."""
    if not result.deadline:
        return ""
    return f"It mentions '{result.deadline}'. Say \"remind me about {result.title[:40] or 'this'}\" to set a reminder."


def digest(results: list[Result], limit: int = 5) -> str:
    """'3 notifications: 1 critical, 1 action required ...' plus the few that need you, most urgent first. Duplicates counted once."""
    unique = [r for r in results if not r.duplicate]
    if not unique:
        return "No notifications to summarise."
    counts = {label: sum(1 for r in unique if r.label == label) for label in LABELS}
    head = f"{len(unique)} notification(s): " + ", ".join(f"{n} {label.replace('_', ' ')}" for label, n in counts.items() if n)
    top = [r for label in (CRITICAL, SUSPICIOUS, ACTION, IMPORTANT) for r in unique if r.label == label][:limit]
    lines = [f"- {r.headline()} ({r.app})" + (f" - {r.deadline}" if r.deadline else "") for r in top]
    skipped = counts[ROUTINE] + counts[PROMO]
    if skipped:
        lines.append(f"- plus {skipped} routine / promotional, not shown")
    dups = len(results) - len(unique)
    if dups:
        lines.append(f"- {dups} repeat(s) ignored")
    return head + "\n" + "\n".join(lines)


def classify_stored(rows: list[dict[str, Any]]) -> list[Result]:
    """Classify notifications already in the notification center (phone ones are titled 'App: title')."""
    clf = Classifier()
    out = []
    for row in rows:
        title = _TAG.sub("", str(row.get("title", "")))
        app, _, rest = title.partition(": ")
        if not rest:
            app, rest = str(row.get("source", "")), title
        out.append(clf.classify(app, rest, str(row.get("body", ""))))
    return out


CLASSIFIER = Classifier()          # the one used for live phone notifications (duplicates are remembered for the session)
