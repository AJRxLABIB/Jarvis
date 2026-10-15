"""Document retrieval: exact-word + meaning search, no embedding model, deleted files, and citations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.config.settings import Settings
from app.memory.semantic import SemanticIndex, fuse, keyword_score, query_terms
from app.tools.base import ToolContext
from app.tools.knowledge import AskDocsArgs, ask_documents, semantic_index
from tests.test_v2 import db, fake_embedder


def _docs(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "errors.txt").write_text("Error E4021 means the licence file is missing. Reinstall the licence.")
    (docs / "tender.txt").write_text("The tender budget is 5 crore. The tender deadline is 30 October.")
    return docs


def test_keyword_helpers() -> None:
    assert query_terms("What is the E4021 error?") == ["e4021", "error"]
    assert keyword_score(["e4021", "error"], "Error E4021 happened") == 1.0
    assert keyword_score(["e4021"], "nothing here") == 0.0
    assert keyword_score([], "x") == 0.0
    assert fuse(1.0, 1.0) == 1.0 and fuse(0.0, 1.0) < fuse(1.0, 0.0)  # meaning counts more than a keyword alone


def test_exact_terms_are_found_even_when_the_meaning_score_is_low(tmp_path: Path) -> None:
    index = SemanticIndex(db(tmp_path), fake_embedder(), "fake")

    async def go():
        await index.index_folder(_docs(tmp_path))
        return await index.search("E4021", "document")

    hits = asyncio.run(go())
    assert hits and Path(hits[0].source).name == "errors.txt"  # the fake model knows nothing about E4021


def test_search_still_works_without_an_embedding_model(tmp_path: Path) -> None:
    index = SemanticIndex(db(tmp_path), fake_embedder(), "fake")
    asyncio.run(index.index_folder(_docs(tmp_path)))

    async def broken(texts):
        raise RuntimeError("Ollama is not running")

    index.embed = broken
    hits = asyncio.run(index.search("licence file missing", "document"))
    assert hits and Path(hits[0].source).name == "errors.txt"


def test_deleted_documents_are_forgotten(tmp_path: Path) -> None:
    docs = _docs(tmp_path)
    index = SemanticIndex(db(tmp_path), fake_embedder(), "fake")
    asyncio.run(index.index_folder(docs))
    assert index.document_count() == 2
    (docs / "tender.txt").unlink()
    stats = asyncio.run(index.index_folder(docs))
    assert stats["removed"] == 1 and index.document_count() == 1
    hits = asyncio.run(index.search("tender deadline", "document"))
    assert all(Path(h.source).name != "tender.txt" for h in hits)  # it can never be cited again


def test_answers_carry_citable_references(tmp_path: Path) -> None:
    ctx = ToolContext(Settings(), database=db(tmp_path))
    ctx.ai = SimpleNamespace(embed=fake_embedder())
    asyncio.run(semantic_index(ctx).index_folder(_docs(tmp_path)))
    result = asyncio.run(ask_documents(AskDocsArgs(question="tender deadline"), ctx))
    first = result.data["passages"][0]
    assert first["ref"] == "[1]" and first["file"] == "tender.txt" and first["passage_number"] >= 1
    assert "cite" in result.message and "don't contain the answer" in result.message
