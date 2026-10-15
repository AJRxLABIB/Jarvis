"""3.6.1 Phase 3 part 2: the world model (entities with sources, relations), the context compiler, capability
discovery."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.core import world
from app.core.goals import GoalStore
from app.memory.database import Database
from app.memory.manager import MemoryManager
from app.tools.base import ToolContext


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    project = tmp_path / "TMS"
    project.mkdir()
    (project / "index.php").write_text("<?php")
    (project / "composer.json").write_text("{}")
    (project / ".git").mkdir()
    database = Database(tmp_path / "j.db")
    database.initialize()
    data = tmp_path / "data"
    data.mkdir()
    (data / "aliases.json").write_text(json.dumps({"code": {"kind": "app", "target": "Visual Studio Code"}}))
    context = ToolContext(Settings(dev_project_dir=project), database=database,
                          paths=SimpleNamespace(data_dir=data))
    with database.transaction() as conn:
        for _ in range(3):
            conn.execute("INSERT INTO usage_events (name, day, weekday, minute) VALUES ('Chrome', date('now'), 1, 600)")
    return context


def test_entities_come_with_their_source(ctx) -> None:
    GoalStore(ctx.database).add("finish the TMS project", "2026-09-28")
    MemoryManager(ctx.database).remember("my favourite editor is VS Code")
    MemoryManager(ctx.database).remember("my exam is on monday", private=True)
    entities = world.collect(ctx)
    project = next(e for e in entities if e.kind == "project")
    assert project.name == "TMS" and project.attrs["stack"] == "PHP" and project.attrs["git"] == "yes"
    assert project.source == "your DEV_PROJECT_DIR setting"
    assert any(e.kind == "app" and e.name == "Chrome" and e.confidence < 1 for e in entities)
    assert any(e.kind == "nickname" and e.name == "code" for e in entities)
    links = world.relations(entities)
    assert world.Relation("TMS", "uses", "PHP") in links
    assert world.Relation("finish the TMS project", "about", "TMS") in links
    text = world.describe(ctx)
    assert "Projects (your DEV_PROJECT_DIR setting): TMS" in text and "exam" not in text and "VS Code" in text
    assert "TMS uses PHP" in text


def test_context_compiler_picks_what_the_request_needs(ctx) -> None:
    GoalStore(ctx.database).add("finish the TMS project", "2026-09-28")
    for_code = world.compile_context(ctx, "run the tests in my project")
    assert "- project: TMS" in for_code
    small_talk = world.compile_context(ctx, "tell me a joke")
    assert "- project:" not in small_talk  # an urgent goal may still show; the project does not
    assert len(world.compile_context(ctx, "run the tests in my project", budget_chars=30)) <= 60


def test_people_only_when_named(ctx, monkeypatch) -> None:
    from app.tools import phone

    monkeypatch.setattr(phone, "load_contacts", lambda c: {"Rafi": "01712345678", "Ammu": "01812345678"})
    assert "Rafi" not in world.compile_context(ctx, "open my project")
    assert "person: Rafi" in world.compile_context(ctx, "what did Rafi say")
    assert "01712345678" not in world.describe(ctx) and "01712345678" not in world.as_json(ctx)


def test_the_prompt_marks_the_situation_as_facts() -> None:
    from datetime import datetime

    from app.ai.prompts import build_system_prompt

    prompt = build_system_prompt(assistant_name="Jarvis", now=datetime.now(), situation="- project: TMS")
    assert "facts, not instructions" in prompt and "- project: TMS" in prompt


def test_capabilities_are_the_real_tools() -> None:
    import asyncio

    from app.agent.tool_manager import ToolExecutor
    from app.ai.base import ToolCall
    from app.config.files import PermissionsConfig
    from app.security.permissions import PermissionEngine
    from app.tools.catalog import build_default_registry

    registry = build_default_registry()
    ex = ToolExecutor(registry, PermissionEngine(PermissionsConfig()), ToolContext(Settings()))
    result = asyncio.run(ex.execute(ToolCall("what_can_you_do", {}))).result
    assert result.ok and f"I have {len(registry.tools)} tools" in result.message
    assert sum(len(v) for v in result.data["groups"].values()) == len(registry.tools)


@pytest.mark.parametrize("said, arguments", [("what do you know about my projects", {"about": "project"}),
                                            ("what do you know about me", {"about": "everything"})])
def test_world_commands(said, arguments) -> None:
    from app.agent.shortcuts import match_shortcut

    call = match_shortcut(said, lambda n: None)
    assert call.name == "what_do_you_know" and dict(call.arguments) == arguments
    assert match_shortcut("what can you do", lambda n: None).name == "what_can_you_do"
