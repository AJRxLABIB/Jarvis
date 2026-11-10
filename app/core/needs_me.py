"""One answer to "what needs me right now?" across everything JARVIS can already see: mail, WhatsApp, phone notifications, promises, to-dos.

Nothing new is read here - each source is the existing reader (mail triage, the WhatsApp chat list, the notification classifier, the commitments ledger, the
to-do list); this only puts their results in ONE order with the reason for each place. A source that could not be read is named as "not checked" - a quiet
list never silently means "nothing there".

Order (highest first), fixed and explainable rather than learned:
 100 suspicious mail / notification (so you do not act on it by mistake)   90 overdue promise you made      85 critical notification
  80 urgent mail                                                            75 promise due today, to-do overdue
  60 a person wrote on WhatsApp / an action-required notification           55 something you are waiting for is overdue
  40 to-do due today                                                        30 promise due soon
Routine and promotional items are counted, not listed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.core import commitments as C


@dataclass(frozen=True)
class Item:
    score: int
    source: str
    title: str
    why: str


def from_mail(triage: dict[str, Any] | None) -> list[Item]:
    out = []
    for m in (triage or {}).get("items", []):
        who, subject = str(m.get("from", ""))[:40], str(m.get("subject", ""))[:80]
        if m.get("category") == "suspicious":
            out.append(Item(100, "mail", f"{who}: {subject}", "looks suspicious - do not click or reply: " + "; ".join(map(str, m.get("warnings", [])))[:120]))
        elif m.get("category") == "urgent":
            out.append(Item(80, "mail", f"{who}: {subject}", "urgent: " + ("; ".join(map(str, m.get("reasons", [])))[:120] or "marked urgent")))
    return out


def from_whatsapp(chats: list[dict[str, Any]] | None) -> list[Item]:
    return [Item(60, "whatsapp", f"{str(c.get('name', ''))[:40]} ({int(c.get('unread', 0) or 0)} unread)", "a person is waiting for a reply") for c in (chats or [])[:8]]


def from_notifications(results: list[Any] | None) -> list[Item]:
    out = []
    for r in results or []:
        label = str(getattr(r, "label", ""))
        if getattr(r, "duplicate", False):
            continue
        title = f"{getattr(r, 'app', '')}: {getattr(r, 'title', '')}".strip(": ")[:90]
        reasons = "; ".join(map(str, getattr(r, "reasons", [])))[:100]
        if label == "suspicious":
            out.append(Item(100, "notification", title, "looks suspicious: " + reasons))
        elif label == "critical":
            out.append(Item(85, "notification", title, "critical: " + reasons))
        elif label == "action":
            out.append(Item(60, "notification", title, "needs an action" + (f" ({getattr(r, 'deadline', '')})" if getattr(r, "deadline", "") else "")))
    return out


def from_commitments(items: list[dict[str, Any]] | None, today: date) -> list[Item]:
    out = []
    for i in items or []:
        b = C.bucket(i, today)
        text = f"{i['who']}: \"{i['text'][:80]}\""
        if i["kind"] == "i_owe":
            score = {"overdue": 90, "today": 75, "soon": 30}.get(b)
            why = {"overdue": f"you promised this for {i['due']}", "today": "you promised this for today", "soon": f"you promised this for {i['due']}"}.get(b, "")
        else:
            score = {"overdue": 55}.get(b)
            why = f"you asked for this by {i['due']} and have no answer"
        if score:
            out.append(Item(score, "promise", text, why))
    return out


def from_todos(todos: list[dict[str, Any]] | None, today: date) -> list[Item]:
    out = []
    for t in todos or []:
        try:
            due = date.fromisoformat(str(t.get("due") or "")[:10])
        except ValueError:
            continue
        if due < today:
            out.append(Item(75, "to-do", str(t.get("text", ""))[:90], f"overdue since {due.isoformat()}"))
        elif due == today:
            out.append(Item(40, "to-do", str(t.get("text", ""))[:90], "due today"))
    return out


def rank(groups: list[list[Item]], limit: int = 12) -> list[Item]:
    seen, out = set(), []
    for item in sorted((i for g in groups for i in g), key=lambda i: (-i.score, i.source, i.title)):
        key = (item.source, item.title.lower())
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out[:limit]


def describe(items: list[Item], checked: list[str], not_checked: dict[str, str], quiet: dict[str, int] | None = None) -> str:
    lines = []
    if items:
        lines.append(f"{len(items)} thing(s) need you, most pressing first:")
        lines += [f"  {n}. [{i.source}] {i.title} - {i.why}" for n, i in enumerate(items, 1)]
    else:
        lines.append("Nothing needs you right now" + (" in " + ", ".join(checked) if checked else "") + ".")
    if quiet and any(quiet.values()):
        lines.append("Not listed: " + ", ".join(f"{n} {k}" for k, n in quiet.items() if n) + ".")
    if checked:
        lines.append("Checked: " + ", ".join(checked) + ".")
    if not_checked:
        lines.append("NOT checked: " + "; ".join(f"{k} ({v})" for k, v in not_checked.items()) + ".")
    return "\n".join(lines)
