"""3.16.0: the Context Engine - a structured snapshot of what you're doing, with sources and certainty."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import context_engine, modes
from app.core.context_engine import ENGINE, KNOWN, LIKELY, UNKNOWN, parse_window
from app.memory.database import Database
from app.security.permissions import PermissionEngine
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "TMS"
    folder.mkdir()
    (folder / "auth.js").write_text("x")
    return folder


@pytest.fixture()
def ctx(tmp_path: Path, project: Path) -> ToolContext:
    database = Database(tmp_path / "j.db")
    database.initialize()
    return ToolContext(Settings(dev_project_dir=project), database=database)


def window(monkeypatch, title: str, process: str = "code.exe") -> None:
    monkeypatch.setattr(context_engine.ContextEngine, "_window",
                        staticmethod(lambda: SimpleNamespace(title=title, process_name=process)))


def fake_git(monkeypatch, branch: str = "develop", changes: int = 3) -> list[str]:
    calls = []

    def git(folder, ctx):
        calls.append(str(folder))
        return {"branch": branch, "changes": changes}

    monkeypatch.setattr("app.core.projects._git", git)
    return calls


def test_editor_title_gives_app_project_and_file(project: Path) -> None:
    facts = parse_window("● auth.js - TMS - Visual Studio Code", "Code.exe", [project, Path("D:/Other")])
    assert facts["active_app"].value == "VS Code" and facts["active_app"].certainty == KNOWN
    assert facts["project"].value == "TMS" and facts["project"].certainty == LIKELY
    assert facts["current_file"].value == "auth.js" and facts["unsaved_changes"].value is True


def test_unknown_folders_in_titles_are_not_projects(project: Path) -> None:
    facts = parse_window("main.py - SomethingElse - Visual Studio Code", "code.exe", [project])
    assert "project" not in facts and facts["current_file"].value == "main.py"


def test_browser_and_terminal_titles() -> None:
    page = parse_window("Pull request #12 - GitHub - Google Chrome", "chrome.exe", [])
    assert page["active_app"].value == "Chrome" and page["browser_page"].value == "Pull request #12 - GitHub"
    assert parse_window("Windows PowerShell", "powershell.exe", [])["terminal"].value == "Windows PowerShell"


def test_secrets_in_titles_are_redacted() -> None:
    facts = parse_window("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 - Notepad", "notepad.exe", [])
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in facts["active_window"].value


def test_snapshot_combines_window_git_goal_and_approval(ctx, project, monkeypatch) -> None:
    window(monkeypatch, "auth.js - TMS - Visual Studio Code")
    calls = fake_git(monkeypatch)
    from app.core.goals import GoalStore

    GoalStore(ctx.database).add("Finish the TMS login", deadline="2026-10-01")
    ENGINE.pending_approval = lambda: "Push changes to GitHub?"
    snap = ENGINE.snapshot(ctx)
    data = snap.to_dict()
    assert data["project"] == "TMS" and data["git_branch"] == "develop" and data["git_changes"] == 3
    assert "Finish the TMS login" in data["current_goal"] and data["pending_approval"] == "Push changes to GitHub?"
    assert data["sources"]["git_branch"]["certainty"] == KNOWN and data["sources"]["project"]["certainty"] == LIKELY
    line = snap.line()
    assert "project TMS (branch develop, 3 uncommitted change(s))" in line and "file auth.js" in line
    ENGINE.snapshot(ctx)
    assert len(calls) == 1  # git is cached, not run on every snapshot
    assert 0 < snap.confidence <= 1


def test_private_mode_reads_no_window_titles(ctx, monkeypatch) -> None:
    window(monkeypatch, "secret.docx - Word", "winword.exe")
    modes.STATE.current = modes.MODES["private"]
    snap = ENGINE.snapshot(ctx)
    assert snap.facts["active_window"].certainty == UNKNOWN and "Private mode" in snap.facts["active_window"].source
    assert "secret" not in str(snap.to_dict())


def test_setting_turns_window_titles_off(ctx, monkeypatch) -> None:
    window(monkeypatch, "auth.js - TMS - Visual Studio Code")
    ctx.settings = ctx.settings.model_copy(update={"context_window_titles": False})
    assert ENGINE.snapshot(ctx).facts["active_window"].certainty == UNKNOWN


def test_no_window_says_why(ctx, monkeypatch) -> None:
    monkeypatch.setattr(context_engine.ContextEngine, "_window", staticmethod(lambda: None))
    fact = ENGINE.snapshot(ctx).facts["active_window"]
    assert fact.certainty == UNKNOWN and "no window" in fact.source


def test_recent_project_is_remembered_briefly_and_only_in_memory(ctx, project, monkeypatch) -> None:
    fake_git(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(ENGINE, "_clock", lambda: clock[0])
    window(monkeypatch, "auth.js - TMS - Visual Studio Code")
    ENGINE.snapshot(ctx)
    window(monkeypatch, "Inbox - Google Chrome", "chrome.exe")
    clock[0] += 30
    snap = ENGINE.snapshot(ctx)
    assert snap.value("project") == "TMS" and "a moment ago" in snap.facts["project"].source
    clock[0] += context_engine.FRESH_SECONDS + 1
    assert ENGINE.recent_project() is None
    clock[0] += context_engine.RETENTION_SECONDS
    ENGINE.snapshot(ctx)
    assert len(ENGINE.history()) == 1  # older snapshots are gone


def test_run_the_tests_means_the_project_you_are_editing(ctx, project, tmp_path, monkeypatch) -> None:
    from app.tools import developer

    other = tmp_path / "Other"
    other.mkdir()
    ctx.settings = ctx.settings.model_copy(update={"dev_project_dir": other})
    fake_git(monkeypatch)
    monkeypatch.setattr(context_engine.ContextEngine, "_projects", lambda self, c: [project, other])
    assert developer.project_folder(None, ctx) == other  # nothing seen yet: the setting
    window(monkeypatch, "auth.js - TMS - Visual Studio Code")
    ENGINE.snapshot(ctx)
    assert developer.project_folder(None, ctx) == project
    assert developer.project_folder(str(other), ctx) == other  # a named folder always wins


def test_current_context_tool_and_its_privacy_class(ctx, monkeypatch) -> None:
    window(monkeypatch, "auth.js - TMS - Visual Studio Code")
    fake_git(monkeypatch)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx)
    result = asyncio.run(ex.execute(ToolCall("current_context", {}))).result
    assert result.ok and "Project: TMS (probably)" in result.message and "Git branch: develop" in result.message
    assert "sources" not in result.data
    full = asyncio.run(ex.execute(ToolCall("current_context", {"detail": "full"}))).result
    assert full.data["sources"]["project"]["certainty"] == "likely"
    from app.security.taint import PRIVATE_TOOLS

    assert "current_context" in PRIVATE_TOOLS


@pytest.mark.parametrize("text", ["what am I working on", "what am i doing right now", "show my current context",
                                  "where was I up to"])
def test_context_shortcuts(text) -> None:
    assert match_shortcut(text, lambda n: None).name == "current_context"


def test_conversation_prompt_gets_the_context_line(ctx, monkeypatch) -> None:
    from app.agent.conversation import ConversationManager

    window(monkeypatch, "auth.js - TMS - Visual Studio Code")
    fake_git(monkeypatch)
    conversation = ConversationManager(SimpleNamespace(), Settings(), tools=SimpleNamespace(context=ctx))
    conversation._current_text = "run the tests"
    situation = conversation._situation()
    assert situation.startswith("Right now: user is in VS Code; project TMS") and ENGINE.last_request == "run the tests"


def test_context_panel_lists_sources(ctx, monkeypatch) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from app.ui.context_panel import ContextPanel

    QApplication.instance() or QApplication([])
    window(monkeypatch, "auth.js - TMS - Visual Studio Code")
    fake_git(monkeypatch)
    panel = ContextPanel(ctx)
    cells = [panel.table.item(r, 0).text() for r in range(panel.table.rowCount())]
    assert "project" in cells and "git branch" in cells and "Right now" in panel.summary.text()
    panel.close()
