"""Second opinion by agreement: ask the same question a few times and keep the answer most runs agree on.

A small model's single answer to a hard question can be a lucky or unlucky sample. Asking again and comparing is an old, simple check:
``agree`` groups answers that say the same thing (same numbers, or mostly the same content words) and returns the largest group; ``ask_until``
stops early as soon as two runs agree (so an easy question costs two calls, not three). When NO two answers agree the result says so - that
disagreement is the useful signal: the model does not know, and the answer should not be trusted.

Limits: agreement is not correctness (a model can be consistently wrong), it multiplies the time per answer, and it is for answers in words or
numbers - never for actions (a tool call is not repeated to "vote").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable

_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")
_STOP = frozenset("the and for with that this from what when how are you your was were has have not but can will would there their about into than then".split())
SAME = 0.55


@dataclass
class Consensus:
    answer: str
    votes: int
    asked: int
    agreed: bool
    note: str

    def text(self) -> str:
        if not self.agreed:
            return f"The {self.asked} answers did not agree with each other - treat this as UNCERTAIN. One of them: {self.answer}"
        return f"{self.answer}\n({self.votes} of {self.asked} runs agreed. Agreement is not proof.)"


def _key(text: str) -> tuple[frozenset[str], frozenset[str]]:
    low = (text or "").lower()
    return frozenset(w for w in _WORD.findall(low) if w not in _STOP), frozenset(n.replace(",", ".") for n in _NUM.findall(low))


def same(a: str, b: str) -> bool:
    """Do two answers say the same thing? Different numbers = different answers, whatever the words."""
    wa, na = _key(a)
    wb, nb = _key(b)
    if na or nb:
        if na != nb:
            return False
        if not wa or not wb:
            return True
    if not wa or not wb:
        return (a or "").strip().lower() == (b or "").strip().lower()
    return len(wa & wb) / len(wa | wb) >= SAME or wa <= wb or wb <= wa


def agree(answers: list[str]) -> Consensus:
    answers = [a.strip() for a in answers if a and a.strip()]
    if not answers:
        return Consensus("", 0, 0, False, "no answers")
    groups: list[list[str]] = []
    for a in answers:
        for g in groups:
            if same(g[0], a):
                g.append(a)
                break
        else:
            groups.append([a])
    best = max(groups, key=len)
    agreed = len(best) >= 2 and len(best) * 2 > len(answers)
    pick = min(best, key=len)                       # the shortest member states the shared content with the least padding
    return Consensus(pick, len(best), len(answers), bool(agreed), "majority" if agreed else "no majority")


async def ask_until(ask: Callable[[], Awaitable[str]], max_runs: int = 3) -> Consensus:
    """Ask up to ``max_runs`` times; stop as soon as two answers agree. A failing run is skipped, not counted as an answer."""
    answers: list[str] = []
    for _ in range(max(2, min(max_runs, 5))):
        try:
            answers.append(await ask())
        except Exception:
            continue
        c = agree(answers)
        if c.agreed:
            return c
    return agree(answers)
