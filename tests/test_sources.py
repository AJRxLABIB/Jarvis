"""Retrieval sources: exclusions (indexing + answering + purge), source switches, adapters (ics/html/Keep), near-duplicates."""

import asyncio
import json
from pathlib import Path

import pytest

from app.memory import sources as S
from app.memory.semantic import SemanticIndex
from tests.test_v2 import db, fake_embedder


def make(tmp_path: Path):
    d = db(tmp_path)
    return d, SemanticIndex(d, fake_embedder(), "fake")


def test_exclusion_matching():
    pats = ["D:/Private/*", "*.bak", "secret-folder"]
    assert S.is_excluded("D:\\Private\\a\\b.txt", pats)
    assert S.is_excluded("C:/x/old.bak", pats)
    assert S.is_excluded("C:/x/secret-folder/n.txt", pats)
    assert not S.is_excluded("C:/x/notes.txt", pats)


def test_excluded_files_are_not_indexed_and_purge_removes_old_ones(tmp_path):
    d, index = make(tmp_path)
    docs = tmp_path / "docs"
    (docs / "private").mkdir(parents=True)
    (docs / "a.txt").write_text("The quarterly budget is large and the tender deadline is October.")
    (docs / "private" / "b.txt").write_text("Private diary: my budget secret plan is here.")
    asyncio.run(index.index_folder(docs))
    assert index.document_count() == 2
    S.add_exclusion(d, str(docs / "private" / "*").replace("\\", "/"))
    hits = asyncio.run(index.search("budget", "document"))
    assert all("private" not in h.source for h in hits) and hits        # stops appearing at once
    assert S.purge_excluded(d) == 1 and index.document_count() == 1
    stats = asyncio.run(index.index_folder(docs))
    assert stats["excluded"] == 1 and index.document_count() == 1
    S.remove_exclusion(d, str(docs / "private" / "*"))
    assert S.exclusions(d) == []


def test_source_switches(tmp_path):
    d, _ = make(tmp_path)
    assert S.set_kind(d, "document", False) == {"document"}
    assert S.disabled_kinds(d) == {"document"}
    assert S.set_kind(d, "document", True) == set()
    with pytest.raises(Exception):
        S.set_kind(d, "email", False)


def test_adapters(tmp_path):
    ics = tmp_path / "cal.ics"
    ics.write_text("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Thesis meeting\r\nDTSTART:20261103T100000\r\nLOCATION:Room 4\r\n"
                   "DESCRIPTION:Bring the draft\\, chapter 3\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    assert S.read_adapter(ics) == "Thesis meeting - 2026-11-03 10:00 - Room 4 - Bring the draft, chapter 3"
    page = tmp_path / "p.html"
    page.write_text("<html><style>x{}</style><script>evil()</script><h1>Title</h1><p>Hello &amp; welcome</p></html>")
    text = S.read_adapter(page)
    assert "Hello & welcome" in text and "evil" not in text and "<" not in text
    keep = tmp_path / "n.json"
    keep.write_text(json.dumps({"title": "Todo", "textContent": "buy milk", "listContent": [{"text": "eggs"}]}))
    assert S.read_adapter(keep) == "Todo\nbuy milk\neggs"
    other = tmp_path / "c.json"
    other.write_text(json.dumps({"api_token": "abc"}))
    assert S.read_adapter(other) is None
    assert S.read_adapter(tmp_path / "x.txt") is None


def test_calendar_file_is_searchable(tmp_path):
    d, index = make(tmp_path)
    docs = tmp_path / "d"
    docs.mkdir()
    (docs / "c.ics").write_text("BEGIN:VEVENT\nSUMMARY:Dentist appointment\nDTSTART:20261105T090000\nEND:VEVENT\n")
    asyncio.run(index.index_folder(docs))
    hits = asyncio.run(index.search("Dentist", "document"))
    assert hits and "Dentist appointment" in hits[0].text


def test_near_duplicates(tmp_path):
    d, index = make(tmp_path)
    docs = tmp_path / "d"
    docs.mkdir()
    base = "The committee approved the new research budget for the laboratory after a long discussion about equipment costs and staffing needs."
    (docs / "a.txt").write_text(base)
    (docs / "copy.txt").write_text(base + " Thanks.")
    (docs / "other.txt").write_text("Completely different words about gardening, tomatoes, watering schedules and soil acidity in spring.")
    asyncio.run(index.index_folder(docs))
    pairs = S.near_duplicates(d)
    assert len(pairs) == 1 and {Path(pairs[0][0]).name, Path(pairs[0][1]).name} == {"a.txt", "copy.txt"} and pairs[0][2] >= 0.85
    assert S.similarity("a b c d e f g", "completely different text here now") < 0.1


def test_ranking_drops_near_copies():
    from app.core.unified_search import rank
    from app.memory.semantic import Hit

    a = "The committee approved the new research budget for the laboratory after discussion of equipment costs and staffing."
    docs = [Hit("document", "x/a.txt", 0, a, 0.9, ""), Hit("document", "x/b.txt", 0, a + " Thanks.", 0.8, ""),
            Hit("document", "x/c.txt", 0, "Budget for the garden: tomatoes and soil acidity are the main costs this spring season.", 0.7, "")]
    out = rank("budget", [], docs, [])
    assert [r.where for r in out] == ["a.txt", "c.txt"]


def test_tool_actions(tmp_path):
    from app.config.settings import Settings
    from app.tools.base import ToolContext
    from app.tools.retrieval_tool import SourcesArgs, sources_tool

    d, _ = make(tmp_path)
    ctx = ToolContext(Settings(), database=d)
    run = lambda **kw: sources_tool(SourcesArgs(**kw), ctx)
    assert "Excluded: none" in run().message
    assert "D:/x/*" in run(action="exclude", value="D:\\x\\*").message
    assert run(action="disable", value="memory").data["disabled"] == ["memory"]
    assert run(action="duplicates").data["pairs"] == []
    assert run(action="purge").data["removed"] == 0
