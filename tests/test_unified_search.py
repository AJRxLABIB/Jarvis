from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.config.settings import Settings
from app.core.unified_search import rank
from app.memory.semantic import Hit
from app.tools.base import ToolContext
from app.tools.knowledge import SearchAllArgs, search_everything, semantic_index
from tests.test_retrieval import _docs
from tests.test_v2 import db, fake_embedder


def hit(kind: str, source: str, text: str, score: float) -> Hit:
    return Hit(kind, source, 0, text, score, "2026-10-01T10:00:00")


def test_rank_puts_trusted_and_relevant_first_and_drops_weak_and_duplicates() -> None:
    memories = [SimpleNamespace(id=3, content="My tender deadline is 30 October"),
                SimpleNamespace(id=4, content="I like tea")]
    docs = [hit("document", "D:/w/tender.txt", "The tender deadline is 30 October.", 0.7),
            hit("document", "D:/w/other.txt", "weakly related text", 0.1)]
    talk = [hit("history", "", "my tender deadline is 30 october", 0.7)]  # same words as the memory: de-duplicated
    results = rank("tender deadline", memories, docs, talk)
    assert [r.source for r in results] == ["memory", "document"]
    assert results[0].where == "memory #3" and results[1].where == "tender.txt"
    assert all(r.score >= 0.25 for r in results)


def test_rank_limit_and_empty() -> None:
    assert rank("anything", [], [], []) == []
    docs = [hit("document", f"a{i}.txt", f"passage {i}", 0.9 - i * 0.01) for i in range(10)]
    assert len(rank("passage", [], docs, [], limit=3)) == 3


def test_search_everything_combines_sources(tmp_path: Path) -> None:
    ctx = ToolContext(Settings(), database=db(tmp_path))
    ctx.ai = SimpleNamespace(embed=fake_embedder())
    ctx.get_memory = lambda: SimpleNamespace(search=lambda q: [SimpleNamespace(id=1, content="The tender budget is 5 crore")])
    asyncio.run(semantic_index(ctx).index_folder(_docs(tmp_path)))
    result = asyncio.run(search_everything(SearchAllArgs(query="tender budget"), ctx))
    sources = {r["source"] for r in result.data["results"]}
    assert result.ok and {"memory", "document"} <= sources
    assert "where each fact came from" in result.message


def test_search_everything_survives_missing_embedding_model(tmp_path: Path) -> None:
    ctx = ToolContext(Settings(), database=db(tmp_path))
    ctx.ai = None  # semantic search unavailable
    ctx.get_memory = lambda: SimpleNamespace(search=lambda q: [SimpleNamespace(id=2, content="Wi-Fi password hint is blue")])
    result = asyncio.run(search_everything(SearchAllArgs(query="wifi hint blue"), ctx))
    assert result.ok and result.data["results"][0]["source"] == "memory" and len(result.data["notes"]) == 2
