"""One shared vocabulary for "how sure are we?" across vision, media, location, sound and agent results.

A ``Fact`` says WHAT is claimed, WHERE it came from and HOW MUCH it may be trusted:

* ``verified``   - measured or read by Jarvis itself right now (an OCR line, a pixel difference, a transcript segment with its time)
* ``user``       - the user said it (kept as theirs, not promoted to verified)
* ``historical`` - was true when recorded; may be out of date (shown with its age)
* ``model``      - a model's interpretation or description: a guess
* ``external``   - an outside agent, a website or a document said it: untrusted

``ContextBook.render`` prints each fact under its label, strongest first, so a prompt or a reply can never present a model's guess as a verified fact. Facts expire
(``ttl_s``): a stale observation is dropped from the "current" view rather than shown as present. Nothing here stores anything: the caller holds the book.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

KINDS = ("verified", "user", "historical", "model", "external")
LABEL = {"verified": "Verified by Jarvis", "user": "Told by you", "historical": "Earlier information (may be out of date)", "model": "Model interpretation (a guess)",
         "external": "From an outside source (untrusted)"}


@dataclass(frozen=True)
class Fact:
    kind: str
    text: str
    source: str
    at: float
    ttl_s: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if not self.text.strip() or not self.source.strip():
            raise ValueError("a fact needs text and a source")


@dataclass
class ContextBook:
    clock: Callable[[], float] = time.time
    facts: list[Fact] = field(default_factory=list)
    limit: int = 60

    def add(self, kind: str, text: str, source: str, ttl_s: float | None = None) -> Fact:
        fact = Fact(kind, text.strip()[:600], source.strip()[:60], self.clock(), ttl_s)
        self.facts = (self.facts + [fact])[-self.limit:]                      # bounded
        return fact

    def current(self) -> list[Fact]:
        now = self.clock()
        return [f for f in self.facts if f.ttl_s is None or now - f.at <= f.ttl_s]

    def render(self) -> str:
        groups = {k: [f for f in self.current() if f.kind == k] for k in KINDS}
        out = []
        for k in KINDS:
            if groups[k]:
                out.append(LABEL[k] + ":")
                out += [f"  - {f.text} [{f.source}{', ' + str(int(self.clock() - f.at)) + ' s ago' if k == 'historical' else ''}]" for f in groups[k]]
        return "\n".join(out) or "Nothing is known for this request yet."

    def only_trusted(self) -> list[Fact]:
        """What may be stated as fact: verified and user-told. Models, outside sources and old information are excluded."""
        return [f for f in self.current() if f.kind in ("verified", "user")]
