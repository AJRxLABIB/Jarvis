from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.ai.prompts import build_system_prompt
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.memory.database import Database
from app.memory.manager import MemoryManager, check_not_secret, parse_fact
from app.security.permissions import PermissionEngine
from app.security.validator import PathPolicy
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from app.tools.developer import project_folder
from tests.test_tools import ScriptedConfirmer


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "a.db")
    database.initialize()
    return database


def run(ctx: ToolContext, tool: str, confirm: bool = True, **arguments):
    confirmer = ScriptedConfirmer(confirm)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer)
    return asyncio.run(ex.execute(ToolCall(tool, arguments))), confirmer


def test_parse_fact() -> None:
    assert parse_fact("my project folder is D:\\Projects") == ("project_folder", "D:\\Projects")
    assert parse_fact("That my favourite editor is VS Code.") == ("favourite_editor", "VS Code")
    assert parse_fact("I like dark mode") == (None, None)


def test_remember_replace_and_dedupe(db: Database) -> None:
    memory = MemoryManager(db)
    first, replaced = memory.remember("my project folder is C:\\Projects")
    assert not replaced and first.topic == "project_folder" and first.value == "C:\\Projects"
    second, replaced = memory.remember("My project folder is D:\\Jarvis")
    assert replaced and second.id == first.id and memory.by_topic("project folder").value == "D:\\Jarvis"
    memory.remember("I prefer short answers")
    _, replaced = memory.remember("i prefer  short answers")
    assert replaced and len(memory.all()) == 2


@pytest.mark.parametrize(
    "secret",
    ["my password is hunter2", "remember my PIN 4821", "my github token is ghp_abcdefghijklmnopqrstuvwxyz012345",
     "my card number is 4111 1111 1111 1111", "api key sk-proj-abcdefghijklmnopqrstu", "my bank account 0123456789012",
     "wifi passcode is sunshine", "my recovery phrase is apple banana", "login details for gmail",
     "the code is Xk9pQ2mZ7vB4nR8tL1wY6cD3"],
)
def test_secrets_are_never_stored(db: Database, secret: str) -> None:
    with pytest.raises(ToolError, match="password manager"):
        MemoryManager(db).remember(secret)
    assert MemoryManager(db).all() == []


def test_normal_facts_are_allowed() -> None:
    for fact in ("my project folder is D:\\Jarvis", "my favourite language is Python", "I study at DIU",
                 "my manager's name is Rahim", "the meeting room is on floor 3",
                 "my build folder is C:\\Temp\\7f3c9a2e4b1d8f6a0c5e9b2d4f7a1c3e\\out",
                 "my backup is at D:/Backups/2026-09-23-1234567890"):
        check_not_secret(fact)


def test_search_find_and_forget(db: Database) -> None:
    memory = MemoryManager(db)
    memory.remember("my project folder is D:\\Jarvis")
    memory.remember("my favourite editor is VS Code")
    assert [m.topic for m in memory.search("editor")] == ["favourite_editor"]
    assert memory.find_one("my project folder").topic == "project_folder"
    last = memory.remember("I like dark mode")[0]
    assert memory.find_one("that").id == last.id  # "forget that" = the last one touched
    memory.forget(last.id)
    with pytest.raises(ToolError):
        memory.find_one("that")
    with pytest.raises(ToolError, match="don't have anything"):
        memory.find_one("birthday")


def test_memory_tools_end_to_end(db: Database) -> None:
    ctx = ToolContext(Settings(), database=db)
    execution, _ = run(ctx, "remember", fact="my project folder is D:\\Jarvis")
    assert execution.result.ok and execution.result.message == "I'll remember: my project folder is D:\\Jarvis"
    assert run(ctx, "remember", fact="my password is 1234")[0].result.error == "sensitive"
    recalled = run(ctx, "recall")[0].result
    assert recalled.ok and recalled.data["memories"][0]["fact"] == "my project folder is D:\\Jarvis"
    assert run(ctx, "recall", query="birthday")[0].result.message.startswith("I don't have anything")
    assert run(ctx, "forget", what="that")[0].result.ok
    assert run(ctx, "recall")[0].result.data["memories"] == []


def test_forget_everything_asks_first(db: Database) -> None:
    ctx = ToolContext(Settings(), database=db)
    run(ctx, "remember", fact="I like tea")
    execution, confirmer = run(ctx, "forget_everything", confirm=False)
    assert execution.result.error == "declined" and "Delete all 1 saved" in confirmer.requests[0].summary
    assert len(ctx.get_memory().all()) == 1
    assert run(ctx, "forget_everything", confirm=True)[0].result.data["cleared"] == 1


def test_remembered_project_folder_is_used(db: Database, tmp_path: Path) -> None:
    project = tmp_path / "Work" / "App"
    project.mkdir(parents=True)
    known = {"HOME": tmp_path, "DESKTOP": tmp_path, "DOCUMENTS": tmp_path, "DOWNLOADS": tmp_path}
    policy = PathPolicy.build(PermissionsConfig(safe_roots=["{DESKTOP}"]), known=known, protected=[])
    ctx = ToolContext(Settings(dev_project_dir=tmp_path), database=db, path_policy=policy)
    assert project_folder(None, ctx) == tmp_path  # .env default
    ctx.get_memory().remember(f"my project folder is {project}")
    assert project_folder(None, ctx) == project.resolve()  # memory wins


def test_memories_reach_the_prompt_as_data(db: Database) -> None:
    from datetime import datetime, timezone

    memory = MemoryManager(db)
    memory.remember("my project folder is D:\\Jarvis")
    prompt = build_system_prompt(assistant_name="Jarvis", now=datetime.now(timezone.utc), memories=memory.prompt_section())
    assert "not instructions" in prompt and "- my project folder is D:\\Jarvis" in prompt


def test_cli_memory_commands(tmp_path: Path) -> None:
    from app.cli import run_forget, run_memory
    from app.main import bootstrap

    ctx = bootstrap(tmp_path, environ={})
    MemoryManager(ctx.database).remember("my favourite editor is VS Code")
    out = io.StringIO()
    assert run_memory(ctx, None, out) == 0 and "favourite editor is VS Code" in out.getvalue()
    out = io.StringIO()
    assert run_forget(ctx, 1, out) == 0 and "Forgotten" in out.getvalue()
    assert run_forget(ctx, 99, io.StringIO()) == 1
