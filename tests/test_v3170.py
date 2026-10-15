"""3.17.0: Memory 2.0 - types, confidence, importance, contradiction detection, supersession, explanations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.memory.database import Database
from app.memory.manager import MemoryManager
from app.memory.memory2 import classify, conflict
from app.security.permissions import PermissionEngine
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "j.db")
    database.initialize()
    return database


def run(db, tool, **arguments):
    ctx = ToolContext(Settings(), database=db)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx)
    return asyncio.run(ex.execute(ToolCall(tool, arguments), user_command=True)).result


@pytest.mark.parametrize(("text", "kind"), [
    ("my editor is VS Code", "semantic"), ("I use PostgreSQL for TMS", "semantic"),
    ("when I start work, open VS Code and Spotify", "procedural"), ("I usually commit before lunch", "procedural"),
    ("I finished the TMS login yesterday", "episodic"), ("my goal is to learn Rust this year", "goal"),
    ("I want to finish the thesis by December", "goal"),
])
def test_memory_types(text, kind) -> None:
    assert classify(text) == kind


@pytest.mark.parametrize(("new", "old", "relation"), [
    ("I migrated the TMS project to PostgreSQL", "The TMS project uses MongoDB", "supersedes"),
    ("TMS moved from MongoDB to PostgreSQL", "TMS uses MongoDB", "supersedes"),
    ("I no longer use Chrome, I use Firefox now", "I use Chrome", "supersedes"),
    ("I use PostgreSQL for TMS", "TMS uses MongoDB", "contradicts"),
    ("I prefer Firefox", "I use Chrome", "contradicts"),
    ("My blog uses MySQL", "TMS uses MongoDB", None),  # different projects: both true
    ("I use VS Code and Chrome", "I use Chrome", None),
    ("I like tea", "I use Chrome", None),
])
def test_contradiction_rules(new, old, relation) -> None:
    verdict = conflict(new, old)
    assert (verdict[0] if verdict else None) == relation


def test_superseded_memory_is_kept_but_never_used(db) -> None:
    manager = MemoryManager(db)
    old, _ = manager.remember("The TMS project uses MongoDB")
    new, _ = manager.remember("I migrated the TMS project to PostgreSQL")
    assert manager.last_conflicts[0]["relation"] == "supersedes"
    assert manager.get(old.id).superseded_by == new.id
    assert old.id not in [m.id for m in manager.all()] and old.id in [m.id for m in manager.all(include_hidden=True)]
    assert "MongoDB" not in manager.prompt_section() and "PostgreSQL" in manager.prompt_section()
    why = manager.explain(manager.get(old.id))
    assert "out of date" in why and "PostgreSQL" in why
    assert "It replaced \"The TMS project uses MongoDB\"" in manager.explain(new)


def test_open_contradiction_and_its_resolution(db) -> None:
    manager = MemoryManager(db)
    old, _ = manager.remember("TMS uses MongoDB")
    new, _ = manager.remember("I use PostgreSQL for TMS")
    conflicts = manager.conflicts()
    assert len(conflicts) == 1 and conflicts[0]["old"] == "TMS uses MongoDB"
    assert "may contradict" in manager.explain(new)
    manager.resolve_conflict(conflicts[0]["id"], "new")
    assert manager.get(old.id).superseded_by == new.id and not manager.conflicts()


def test_remember_tool_reports_conflicts_and_shortcut_settles_them(db) -> None:
    run(db, "remember", fact="I use Chrome")
    result = run(db, "remember", fact="I prefer Firefox")
    assert result.ok and "may contradict \"I use Chrome\"" in result.message and "keep the new one" in result.message
    assert match_shortcut("keep the new one", lambda n: None).arguments == {"keep": "new"}
    assert match_shortcut("both are true", lambda n: None).arguments == {"keep": "both"}
    settled = run(db, "resolve_memory_conflict", keep="both")
    assert settled.ok and "both stay" in settled.message
    assert not run(db, "resolve_memory_conflict", keep="new").ok  # nothing left to settle
    assert run(db, "memory_conflicts").message.startswith("No memory contradicts")


def test_stated_vs_recorded_confidence_and_importance(db) -> None:
    manager = MemoryManager(db)
    stated, _ = manager.remember("My thesis deadline is 15 December", importance=3)
    event = manager.record_event("Completed the mission 'Prepare the demo' on 2026-09-26")
    assert stated.stated and stated.confidence == 1.0 and stated.importance == 3
    assert not event.stated and event.kind == "episodic" and event.provenance == "event"
    explanation = manager.explain(event)
    assert "you didn't say it" in explanation and "something that happened" in explanation
    assert "you said it" in manager.explain(stated)
    assert manager.prompt_section().splitlines()[0].startswith("- My thesis deadline")  # most important first


def test_forgetting_removes_links_and_conflicts(db) -> None:
    manager = MemoryManager(db)
    manager.remember("TMS uses MongoDB")
    new, _ = manager.remember("I use PostgreSQL for TMS")
    manager.forget(new.id)
    assert not manager.conflicts()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0] == 0


def test_secrets_are_still_refused(db) -> None:
    result = run(db, "remember", fact="my password is hunter2hunter2")
    assert not result.ok and result.error == "sensitive"


def test_finished_mission_becomes_an_episodic_memory(db) -> None:
    from types import SimpleNamespace

    from app.ui.controller_parts.proactive import ProactiveMixin

    fake = SimpleNamespace(ctx=SimpleNamespace(database=db))
    ProactiveMixin._remember_mission(fake, SimpleNamespace(data={"title": "Prepare the demo"}))
    memories = MemoryManager(db).all()
    assert memories[0].kind == "episodic" and "Prepare the demo" in memories[0].content and not memories[0].stated
