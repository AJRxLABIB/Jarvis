"""One ranked answer across what you told Jarvis (memories), your documents, and past conversations.

Each source scores in its own way, so the scores are put on one scale before ranking:

* a saved memory is something you said about yourself - it gets a trust boost, and its score is the share of the
  question's words it contains (memory search is word-based);
* a document passage keeps its hybrid (meaning + words) score;
* a past conversation exchange is second-hand, so it is slightly discounted.

Every result keeps its source, so the answer can say where each fact came from. Pure functions: the tool gathers
the candidates, this module ranks them (and is easy to test).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.memory.semantic import Hit, keyword_score, query_terms

MEMORY_BOOST = 0.15
HISTORY_FACTOR = 0.85
MIN_SCORE = 0.25


@dataclass(frozen=True)
class Result:
    source: str          # "memory" | "document" | "conversation"
    text: str
    score: float
    where: str           # memory #id, file name, or a date
    extra: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "where": self.where, "text": self.text[:700], "score": round(self.score, 2),
                **self.extra}


def rank(query: str, memories: Sequence[Any], documents: Sequence[Hit], history: Sequence[Hit],
         limit: int = 8) -> list[Result]:
    """Rank candidates from the three sources. ``memories`` are objects with ``id`` and ``content``."""
    terms = query_terms(query)
    found: list[Result] = []
    for memory in memories:
        base = keyword_score(terms, memory.content)
        if base > 0:
            found.append(Result("memory", memory.content, min(1.0, base + MEMORY_BOOST), f"memory #{memory.id}",
                                {"memory_id": memory.id}))
    for hit in documents:
        found.append(Result("document", hit.text, hit.score, hit.source.replace("\\", "/").rsplit("/", 1)[-1],
                            {"path": hit.source, "passage_number": hit.chunk + 1}))
    for hit in history:
        found.append(Result("conversation", hit.text, hit.score * HISTORY_FACTOR, (hit.created_at or "")[:10],
                            {}))
    from app.memory.sources import similarity

    seen: set[str] = set()
    ranked: list[Result] = []
    for item in sorted(found, key=lambda r: r.score, reverse=True):
        key = " ".join(item.text.lower().split())[:160]
        if item.score < MIN_SCORE or key in seen:
            continue
        if any(similarity(item.text, r.text) >= 0.85 for r in ranked):      # the same text again (a copy or a light edit)
            continue
        seen.add(key)
        ranked.append(item)
        if len(ranked) >= limit:
            break
    return ranked
