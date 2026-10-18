from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.core.research_store import ResearchStore, normalise_url
from app.tools import research as r
from app.tools.base import ToolContext, ToolError
from tests.test_v2 import db


@pytest.fixture()
def store(tmp_path: Path) -> ResearchStore:
    s = ResearchStore(db(tmp_path))
    s.create("Laptops", "Which laptop for coding?", "price, weight")
    return s


def test_urls_are_normalised_and_deduplicated(store: ResearchStore) -> None:
    assert normalise_url("HTTPS://Example.com/a/?utm_source=x&id=2#top") == "https://example.com/a?id=2"
    with pytest.raises(ToolError):
        normalise_url("ftp://x")
    first = store.add_source("Laptops", "https://example.com/review?utm_campaign=z", "Model A costs 900 dollars", "Review")
    again = store.add_source("Laptops", "https://example.com/review", "Model A costs 900 dollars")
    assert first["new"] and not again["new"] and first["id"] == again["id"]
    copy = store.add_source("Laptops", "https://mirror.example.org/review", "model a costs 900   dollars")
    assert copy["new"] and copy["duplicate_of"] == "https://example.com/review"


def test_sourced_claims_need_a_source_but_inference_does_not(store: ResearchStore) -> None:
    s = store.add_source("Laptops", "https://a.example/x", "text")
    with pytest.raises(ToolError, match="must point at"):
        store.add_claim("Laptops", "price", "Costs 900", None)
    with pytest.raises(ToolError):
        store.add_claim("Laptops", "price", "Costs 900", 9999)
    store.add_claim("Laptops", "price", "Costs 900", s["id"])
    store.add_claim("Laptops", "verdict", "Probably the best value", kind="inference")
    text = store.report("Laptops")
    assert "Sourced facts:" in text and "Costs 900 [1]" in text
    assert "Inference (analysis, not sourced fact):" in text and "best value" in text


def test_numeric_conflicts_are_flagged_not_resolved(store: ResearchStore) -> None:
    a = store.add_source("Laptops", "https://a.example/x", "aaa")
    b = store.add_source("Laptops", "https://b.example/y", "bbb")
    store.add_claim("Laptops", "price", "Model A costs 900 dollars", a["id"])
    store.add_claim("Laptops", "price", "Model A costs 1,100 dollars", b["id"])
    store.add_claim("Laptops", "weight", "It weighs 1.4 kg", a["id"])
    store.add_claim("Laptops", "weight", "It is light", b["id"])  # no numbers: no claim of conflict
    conflicts = store.conflicts(store.project("Laptops")["id"])
    assert len(conflicts) == 1 and conflicts[0]["topic"] == "price"
    assert "Possible conflicts" in store.report("Laptops")


def test_refresh_separates_old_from_new(store: ResearchStore) -> None:
    a = store.add_source("Laptops", "https://a.example/x", "price 900")
    store.add_source("Laptops", "https://b.example/y", "stable text")
    store.add_source("Laptops", "https://c.example/z", "gone")
    cid = store.add_claim("Laptops", "price", "Costs 900", a["id"])
    report = store.refresh("Laptops", {"https://a.example/x": "price 950", "https://b.example/y": "stable  TEXT",
                                       "https://c.example/z": None})
    assert report["changed"] == ["https://a.example/x"] and report["unchanged"] == ["https://b.example/y"]
    assert report["unreadable"] == ["https://c.example/z"]
    assert "needs recheck" in store.report("Laptops")
    store.confirm_claim(cid)
    assert "needs recheck" not in store.report("Laptops")


def test_projects_conclusion_and_delete(store: ResearchStore) -> None:
    with pytest.raises(ToolError, match="already"):
        store.create("laptops", "again")
    store.conclude("Laptops", "Buy model A")
    assert "Conclusion (yours" in store.report("Laptops")
    assert store.projects()[0]["name"] == "Laptops"
    store.delete("Laptops")
    with pytest.raises(ToolError):
        store.project("Laptops")


def test_the_tool_end_to_end(tmp_path: Path, monkeypatch) -> None:
    ctx = ToolContext(Settings(), database=db(tmp_path))

    def call(**kw):
        return asyncio.run(r.research_project(r.ProjectArgs(**kw), ctx))

    call(action="create", project="Fees", question="What are the bank's fees?")
    call(action="add_source", project="Fees", url="https://bank.example/fees", text="Fee is 5 dollars", title="Fees page",
         source_date="2026-09-01")
    call(action="add_claim", project="Fees", topic="monthly fee", claim="The fee is 5 dollars", source_number=1)
    with pytest.raises(ToolError):
        call(action="add_claim", project="Fees", topic="x", claim="no source")
    assert "[1] Fees page" in call(action="show", project="Fees").message

    async def fake_fetch(url):
        return url, "t", "Fee is 7 dollars"

    monkeypatch.setattr(r.pages, "fetch", fake_fetch)
    result = call(action="check_updates", project="Fees")
    assert result.data["changed"] == ["https://bank.example/fees"] and "needs recheck" in result.message

    async def broken(url):
        raise ToolError("offline", "offline")

    monkeypatch.setattr(r.pages, "fetch", broken)
    assert call(action="check_updates", project="Fees").data["unreadable"]
    assert "Fees" in call(action="list").message
