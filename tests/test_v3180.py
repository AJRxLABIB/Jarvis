"""3.18.0: the personal knowledge graph - projects, files, decisions, memories, goals, requests - with evidence."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agent.activity import period
from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import knowledge_graph as kg
from app.memory.database import Database
from app.memory.manager import MemoryManager
from app.security.permissions import PermissionEngine
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry

NOW = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc).astimezone()


@pytest.fixture()
def world(tmp_path: Path):
    tms = tmp_path / "TMS"
    (tms / "app").mkdir(parents=True)
    (tms / "composer.json").write_text(json.dumps({"require": {"php": "^8.2", "laravel/framework": "^11"}}))
    (tms / "index.php").write_text("<?php")
    (tms / "app" / "AuthController.php").write_text("<?php")
    if shutil.which("git"):
        for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"], ["add", "."],
                     ["commit", "-qm", "first"]):
            subprocess.run(["git", *args], cwd=tms, check=True, capture_output=True)
        (tms / "app" / "AuthController.php").write_text("<?php // fixed")
        subprocess.run(["git", "commit", "-qam", "fix auth"], cwd=tms, check=True, capture_output=True)
    database = Database(tmp_path / "j.db")
    database.initialize()
    ctx = ToolContext(Settings(dev_project_dir=tms), database=database, clock=lambda: NOW)
    return ctx, tms


def test_projects_files_and_technologies(world) -> None:
    ctx, _ = world
    graph = kg.build(ctx)
    assert "project:tms" in graph.nodes
    answer, lines = kg.files_of(graph, "TMS")
    assert "index.php (entry point)" in answer
    if shutil.which("git"):
        assert any("app/AuthController.php" in line and "git log" in line for line in lines)
    answer, lines = kg.project_with(graph, "laravel", ctx)
    assert lines and lines[0].startswith("TMS (TMS's project files)")


def test_which_project_contains_a_file(world) -> None:
    ctx, _ = world
    answer, lines = kg.project_with(kg.build(ctx, with_files=False), "AuthController", ctx)
    assert "TMS (file AuthController.php)" in answer


def test_decisions_come_from_memories_with_their_history(world) -> None:
    ctx, _ = world
    manager = MemoryManager(ctx.database)
    manager.remember("The TMS project uses MongoDB")
    manager.remember("We switched TMS from MongoDB to PostgreSQL because of joins")
    manager.remember("I like tea")
    answer, _, kind = kg.ask(ctx, "what decision did I make about the database")
    assert kind == "decisions" and "switched TMS from MongoDB to PostgreSQL" in answer
    assert "uses MongoDB\" (no longer true)" in answer and "tea" not in answer
    answer, _, _ = kg.ask(ctx, "why did we choose PostgreSQL")
    assert "PostgreSQL" in answer and "memory #" in answer


def test_private_and_disabled_memories_stay_out(world) -> None:
    ctx, _ = world
    manager = MemoryManager(ctx.database)
    secret, _ = manager.remember("I decided TMS keeps its admin panel private", private=True)
    graph = kg.build(ctx, with_files=False)
    assert f"memory:{secret.id}" not in graph.nodes


def test_what_was_i_working_on_last_week(world) -> None:
    ctx, tms = world
    last_week = (NOW - timedelta(days=NOW.weekday() + 3)).astimezone(timezone.utc).isoformat(timespec="seconds")
    with ctx.database.transaction() as conn:
        conn.execute("INSERT INTO agent_runs (id, source, text, state, started_at) VALUES ('r1', 'voice', "
                     "'run the TMS tests', 'done', ?)", (last_week,))
        conn.execute("INSERT INTO audit_log (ts, event, tool, details) VALUES (?, 'tool_result', 'run_command', ?)",
                     (last_week, json.dumps({"arguments": {"command": "php artisan test", "project": str(tms)},
                                             "ok": True})))
    answer, lines, kind = kg.ask(ctx, "what was I working on last week")
    assert kind == "worked_on" and answer.startswith("Last week you worked on: TMS (2 request(s)/action(s)")
    empty, _, _ = kg.ask(ctx, "what was I working on yesterday")
    assert "I have no record" in empty and "not work you did by hand" in empty


def test_goals_link_to_projects_and_related_question(world) -> None:
    ctx, _ = world
    from app.core.goals import GoalStore

    GoalStore(ctx.database).add("Finish the TMS login")
    answer, lines, kind = kg.ask(ctx, "everything related to TMS")
    assert kind == "related" and any("goal \"Finish the TMS login\"" in line for line in lines)
    assert any("technology \"laravel/framework\"" in line for line in lines)


def test_unknown_things_say_so(world) -> None:
    ctx, _ = world
    assert kg.ask(ctx, "which files are related to Zeta")[0] == "I don't know a project called Zeta."
    assert "no recorded decision" in kg.ask(ctx, "what decision did I make about the editor")[0]


@pytest.mark.parametrize(("phrase", "label", "days"), [("last week", "last week", 7), ("last month", "last month", None),
                                                       ("this month", "this month", None),
                                                       ("the last 3 days", "in the last 3 days", None)])
def test_new_time_periods(phrase, label, days) -> None:
    start, end, found = period(phrase, NOW)
    assert found == label and start < end
    if days:
        assert (end - start).days == days and start.weekday() == 0


def test_ask_knowledge_tool_is_private_and_answers(world) -> None:
    ctx, _ = world
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx)
    result = asyncio.run(ex.execute(ToolCall("ask_knowledge", {"question": "which project uses laravel"}))).result
    assert result.ok and result.data["question_type"] == "contains" and result.data["evidence"]
    from app.security.taint import PRIVATE_TOOLS

    assert "ask_knowledge" in PRIVATE_TOOLS


def test_knowledge_window(world) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from app.ui.knowledge_dialog import KnowledgeDialog

    QApplication.instance() or QApplication([])
    ctx, _ = world
    dialog = KnowledgeDialog(ctx)
    dialog.question.setText("which project uses laravel")
    dialog.ask()
    assert "TMS" in dialog.answer.text() and dialog.evidence.count() >= 1
    dialog.close()
