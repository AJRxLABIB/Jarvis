"""Memory consolidation, review-first: Jarvis may PROPOSE merging two memories that say nearly the same thing; only you can apply it, and it can be undone.

Why not automatic: letting a model rewrite stored facts unattended can make memory worse than leaving it alone (wrong merges propagate). So:
* ``candidates`` - pairs with high word overlap in the same category; private, disabled, superseded and expiring memories are never touched;
* ``propose``    - the local model is asked for ONE sentence that keeps every fact of both. Its answer is accepted only if it invents nothing:
                   every number and every capitalised name in it must already be in one of the two memories, it must not be longer than both together,
                   and it must still share most of their words. Otherwise the proposal falls back to "keep the longer one" (marked as a rule, not the model);
* ``apply``      - only if both memories still read exactly as when proposed (else the proposal is stale and refused). The first memory gets the merged
                   wording (its old wording stays in the revision history); the second is switched off, not deleted;
* ``undo``       - the first memory gets its old wording back and the second is switched on again.
Proposals live in the settings store (at most ``MAX_OPEN`` open). Nothing here runs without a confirmed tool call.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Callable

OVERLAP = 0.6
MAX_OPEN = 20
KEY = "memory_merge_proposals"
_STOP = {"the", "a", "an", "is", "are", "to", "of", "in", "on", "and", "my", "i", "that", "it", "for", "with", "at", "as", "was", "be", "his", "her", "their"}
PROMPT = ("Merge these two notes into ONE short sentence that keeps every fact from both. Do not add anything that is not in them. "
          "Answer with the sentence only.\nNote 1: {a}\nNote 2: {b}")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[^\W_]+", (text or "").lower()) if w not in _STOP}


def overlap(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, len(wa | wb))


def eligible(m: Any) -> bool:
    return (not getattr(m, "disabled", False) and getattr(m, "sensitivity", "normal") != "private" and getattr(m, "superseded_by", None) is None
            and not getattr(m, "expires_at", None) and getattr(m, "kind", "semantic") != "event")


def candidates(memories: list[Any], limit: int = 10) -> list[tuple[Any, Any, float]]:
    pool = [m for m in memories if eligible(m)]
    out = []
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            if a.category == b.category and a.content.strip().lower() != b.content.strip().lower():
                s = overlap(a.content, b.content)
                if s >= OVERLAP:
                    out.append((a, b, round(s, 2)))
    return sorted(out, key=lambda t: -t[2])[:limit]


def invents(merged: str, a: str, b: str) -> str:
    """'' when the merged sentence adds nothing; otherwise what it added / why it is rejected."""
    src = f"{a} {b}"
    new_numbers = [n for n in re.findall(r"\d+(?:[.,:]\d+)*", merged) if n not in re.findall(r"\d+(?:[.,:]\d+)*", src)]
    if new_numbers:
        return f"new number(s): {', '.join(new_numbers[:3])}"
    src_words = {w.lower() for w in re.findall(r"[^\W_]+", src)}
    names = [w for w in re.findall(r"\b[A-Z][^\W_]+", merged)[1:] if w.lower() not in src_words]      # [1:] - the sentence's first word is capitalised anyway
    if names:
        return f"new name(s): {', '.join(names[:3])}"
    if len(merged) > len(a) + len(b) + 10:
        return "longer than both notes together"
    added = _words(merged) - _words(src)
    if len(added) > 3:
        return f"new words: {', '.join(sorted(added)[:4])}"
    if not (_words(a) | _words(b)) or len(_words(merged) & (_words(a) | _words(b))) / len(_words(a) | _words(b)) < 0.5:
        return "drops too much of the notes"
    return ""


def propose(a: Any, b: Any, generate: Callable[[str], str] | None = None, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    merged, by, why = "", "rule", "no model was used"
    if generate is not None:
        try:
            text = " ".join(str(generate(PROMPT.format(a=a.content, b=b.content)) or "").split()).strip().strip('"')
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
            bad = invents(text, a.content, b.content) if text else "the model gave no answer"
            if not bad:
                merged, by, why = text, "model", "checked: it adds no number, name or new fact"
            else:
                why = f"the model's wording was rejected ({bad})"
        except Exception as exc:
            why = f"the model could not be asked ({type(exc).__name__})"
    if not merged:
        merged = a.content if len(a.content) >= len(b.content) else b.content
    return {"id": uuid.uuid4().hex[:8], "a_id": a.id, "b_id": b.id, "a_text": a.content, "b_text": b.content, "merged": merged, "by": by, "why": why,
            "overlap": overlap(a.content, b.content), "at": clock(), "status": "open"}


class Proposals:
    def __init__(self, store: Any) -> None:
        self.store = store

    def all(self) -> list[dict[str, Any]]:
        return list(self.store.get(KEY, []) or [])

    def open(self) -> list[dict[str, Any]]:
        return [p for p in self.all() if p["status"] == "open"]

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.store.set(KEY, items[-60:])

    def add(self, p: dict[str, Any]) -> bool:
        items = self.all()
        pair = {p["a_id"], p["b_id"]}
        if any({x["a_id"], x["b_id"]} == pair and x["status"] in ("open", "rejected", "applied") for x in items) or len(self.open()) >= MAX_OPEN:
            return False                                           # already proposed (a rejected pair is never proposed again), or the list is full
        self._save(items + [p])
        return True

    def get(self, pid: str) -> dict[str, Any] | None:
        return next((p for p in self.all() if p["id"] == pid), None)

    def set_status(self, pid: str, status: str) -> None:
        items = self.all()
        for p in items:
            if p["id"] == pid:
                p["status"] = status
        self._save(items)


def apply(manager: Any, props: Proposals, pid: str) -> str:
    p = props.get(pid)
    if p is None or p["status"] != "open":
        raise LookupError("no open proposal with that id")
    a, b = manager.get(p["a_id"]), manager.get(p["b_id"])
    if a is None or b is None or a.content != p["a_text"] or b.content != p["b_text"] or not eligible(a) or not eligible(b):
        props.set_status(pid, "stale")
        raise ValueError("one of the two memories changed since this was proposed, so nothing was merged")
    if a.content != p["merged"]:
        manager.edit(a, p["merged"])
    manager.set_disabled(manager.get(b.id), True)
    props.set_status(pid, "applied")
    return f"Merged: memory {a.id} now reads \"{p['merged']}\"; memory {b.id} is switched off (not deleted). Undo with this id: {pid}."


def undo(manager: Any, props: Proposals, pid: str) -> str:
    p = props.get(pid)
    if p is None or p["status"] != "applied":
        raise LookupError("no applied merge with that id")
    a, b = manager.get(p["a_id"]), manager.get(p["b_id"])
    if a is not None and a.content != p["a_text"]:
        manager.edit(a, p["a_text"])
    if b is not None:
        manager.set_disabled(b, False)
    props.set_status(pid, "undone")
    return "Undone: both memories are back as they were."


def scan(manager: Any, props: Proposals, generate: Callable[[str], str] | None = None, limit: int = 3) -> int:
    """Add up to ``limit`` new proposals. Returns how many were added."""
    added = 0
    for a, b, _ in candidates(manager.all(limit=400)):
        if added >= limit:
            break
        if any({x["a_id"], x["b_id"]} == {a.id, b.id} for x in props.all()):
            continue
        if props.add(propose(a, b, generate)):
            added += 1
    return added


def describe(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No merge proposals. Memories are only merged when you approve one."
    return "\n".join(f"[{p['id']}] overlap {p['overlap']:.0%}\n  1: {p['a_text']}\n  2: {p['b_text']}\n  merged ({'the model, checked' if p['by'] == 'model' else 'rule: keep the longer one'}): {p['merged']}\n  note: {p['why']}"
                     for p in items) + "\nNothing is changed until you say 'apply' with an id; 'reject' hides a pair for good."
