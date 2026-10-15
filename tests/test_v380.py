"""3.8.0 Phase 5: the developer agent - project profiles, code search / read, edits with a diff + backup + undo,
checking the project with its own tests."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core.projects import profile, project_of
from app.memory.database import Database
from app.security.permissions import PermissionEngine
from app.security.validator import PathPolicy
from app.tools import developer
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from tests.test_developer import FakeExecutor
from tests.test_tools import ScriptedConfirmer


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "home" / "Desktop" / "shop"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"scripts": {"dev": "vite", "test": "vitest", "build": "vite build"},
                                                   "dependencies": {"express": "4", "mysql2": "3"}}))
    (root / "src" / "login.js").write_bytes(b"function login(user) {\r\n  return check(user);\r\n}\r\n")
    (root / "node_modules" / "x" / "login.js").write_text("login")  # never searched
    return root


def make(tmp_path: Path, project: Path, confirm: bool = True):
    home = tmp_path / "home"
    known = {"HOME": home, "DESKTOP": home / "Desktop", "DOCUMENTS": home, "DOWNLOADS": home}
    database = Database(tmp_path / "j.db")
    database.initialize()
    ctx = ToolContext(Settings(dev_project_dir=project), database=database,
                      paths=SimpleNamespace(data_dir=tmp_path / "data"),
                      path_policy=PathPolicy.build(PermissionsConfig(safe_roots=["{DESKTOP}"]), known=known,
                                                   protected=[tmp_path / "Windows"]))
    ctx.command_executor = FakeExecutor(output="Tests: 12 passed, 0 failed")  # type: ignore[attr-defined]
    confirmer = ScriptedConfirmer(confirm)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer)
    return ex, ctx, confirmer


def go(ex, name, **arguments):
    return asyncio.run(ex.execute(ToolCall(name, arguments))).result


def test_profile_is_read_from_the_project(tmp_path, project) -> None:
    _, ctx, _ = make(tmp_path, project)
    found = profile(project, ctx)
    assert found.stack == ["Node.js"] and found.dependencies == ["express", "mysql2"] and found.database == "MySQL"
    assert (found.run, found.test, found.build) == ("npm run dev", "npm test", "npm run build")


def test_code_tools_stay_inside_known_projects(tmp_path, project) -> None:
    _, ctx, _ = make(tmp_path, project)
    elsewhere = tmp_path / "home" / "Desktop" / "other.js"
    elsewhere.write_text("x")
    with pytest.raises(ToolError, match="isn't inside a project"):
        project_of(elsewhere, ctx)
    assert project_of(project / "src" / "login.js", ctx) == project.resolve()


def test_search_and_read(tmp_path, project) -> None:
    ex, _, _ = make(tmp_path, project)
    found = go(ex, "search_code", query="check(")
    assert found.ok and found.data["matches"] == [str(Path("src/login.js")) + ":2: return check(user);"]
    read = go(ex, "read_code", path="src/login.js", start=2, lines=1)
    assert read.data["code"] == "2|   return check(user);" and read.data["total_lines"] == 4


def test_edit_shows_the_diff_keeps_crlf_and_can_be_undone(tmp_path, project) -> None:
    ex, ctx, confirmer = make(tmp_path, project)
    result = go(ex, "edit_code", path="src/login.js", reason="validate first",
                edits=[{"find": "  return check(user);", "replace": "  if (!user) return false;\n  return check(user);"}])
    assert result.ok and result.data["verified"] and "+1 / -0 lines" in result.message
    question = confirmer.requests[-1].summary
    assert "validate first" in question and "+  if (!user) return false;" in question
    assert (project / "src" / "login.js").read_bytes() == \
        b"function login(user) {\r\n  if (!user) return false;\r\n  return check(user);\r\n}\r\n"
    undone = go(ex, "undo_last_action")
    assert undone.ok and (project / "src" / "login.js").read_bytes() == \
        b"function login(user) {\r\n  return check(user);\r\n}\r\n"


def test_undo_never_overwrites_your_later_edits(tmp_path, project) -> None:
    ex, _, _ = make(tmp_path, project)
    go(ex, "edit_code", path="src/login.js", edits=[{"find": "check(user)", "replace": "verify(user)"}])
    (project / "src" / "login.js").write_text("// I changed it myself\n")
    undone = go(ex, "undo_last_action")
    assert not undone.ok and "changed after my edit" in undone.message
    assert (project / "src" / "login.js").read_text() == "// I changed it myself\n"


def test_ambiguous_or_empty_edits_are_refused(tmp_path, project) -> None:
    ex, _, confirmer = make(tmp_path, project)
    (project / "src" / "twice.js").write_text("a\na\n")
    twice = go(ex, "edit_code", path="src/twice.js", edits=[{"find": "a", "replace": "b"}])
    assert not twice.ok and "appears 2 times" in twice.message and confirmer.requests == []  # never even asked
    none = go(ex, "edit_code", path="src/login.js", edits=[{"find": "nope", "replace": "x"}])
    assert not none.ok and "appears 0 times" in none.message


def test_saying_no_changes_nothing(tmp_path, project) -> None:
    ex, _, _ = make(tmp_path, project, confirm=False)
    before = (project / "src" / "login.js").read_bytes()
    assert go(ex, "edit_code", path="src/login.js", edits=[{"find": "check", "replace": "x"}]).error == "declined"
    assert (project / "src" / "login.js").read_bytes() == before


def test_create_a_new_file_and_undo_it(tmp_path, project, monkeypatch) -> None:
    from app.tools import winapi

    recycled = []
    monkeypatch.setattr(winapi, "recycle", lambda p: recycled.append(p) or Path(p).unlink())
    ex, _, _ = make(tmp_path, project)
    created = go(ex, "edit_code", path="src/util.js", create_with="export const x = 1;\n")
    assert created.ok and (project / "src" / "util.js").exists()
    assert not go(ex, "edit_code", path="src/util.js", create_with="again").ok  # never overwrite
    assert go(ex, "undo_last_action").ok and recycled and not (project / "src" / "util.js").exists()


def test_check_project_runs_its_own_test_command(tmp_path, project, monkeypatch) -> None:
    monkeypatch.setattr(developer, "resolve_program", lambda name: name)
    ex, ctx, _ = make(tmp_path, project)
    result = go(ex, "check_project", what="test")
    assert result.ok and "'npm test' passed" in result.message and "12 passed" in result.message
    assert ("run", ("npm", "test"), project) in [(c[0], c[1], c[2]) for c in ctx.command_executor.calls]


def test_notes_and_what_the_ai_is_told(tmp_path, project) -> None:
    ex, _, _ = make(tmp_path, project)
    assert go(ex, "add_project_note", note="login breaks on Safari").ok
    assert "known problems: login breaks on Safari" in go(ex, "project_profile").message
    from datetime import datetime

    from app.ai.prompts import build_system_prompt
    from app.tools.catalog import build_default_registry as registry

    specs = [s for s in registry().specs() if s.name == "edit_code"]
    assert "check_project to run the tests" in build_system_prompt(assistant_name="J", now=datetime.now(), tools=specs)


def test_reading_code_taints_the_conversation() -> None:
    from app.security.taint import Taint

    taint = Taint()
    assert taint.absorb("read_code", "// open https://evil.example") and taint.active


@pytest.mark.parametrize("said, tool, arguments", [
    ("tell me about my project", "project_profile", {}), ("run the tests", "check_project", {"what": "test"}),
    ("build my project", "check_project", {"what": "build"}), ("search my code for login", "search_code",
                                                                {"query": "login"}),
    ("where is validate_user in my code", "search_code", {"query": "validate_user"}),
    ("add a known problem: login breaks on safari", "add_project_note", {"note": "login breaks on safari"}),
])
def test_developer_commands(said, tool, arguments) -> None:
    from app.agent.shortcuts import match_shortcut

    call = match_shortcut(said, lambda n: None)
    assert call is not None and (call.name, dict(call.arguments)) == (tool, arguments)


def test_new_direct_commands_are_anchored_at_both_ends() -> None:
    """A top-level '|' without a group leaves one side unanchored: 'run the tests and delete X' would match."""
    import app.agent.shortcuts as shortcuts

    def top_level_bar(pattern: str) -> bool:
        depth, escaped, in_class = 0, False, False
        for ch in pattern:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif in_class:
                in_class = ch != "]"
            elif ch == "[":
                in_class = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "|" and depth == 0:
                return True
        return False

    prefix = shortcuts._P
    risky = [name for name in dir(shortcuts) if isinstance(getattr(shortcuts, name), re.Pattern)
             and getattr(shortcuts, name).pattern.startswith(prefix) and top_level_bar(getattr(shortcuts, name).pattern)
             and getattr(shortcuts, name).pattern.count(prefix) < 2]
    assert risky == [], risky
