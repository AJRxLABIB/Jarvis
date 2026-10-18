"""Master prompt 3, stage E: edit a memory in place, import an export (validated), and say which memories were used
for the last answer and why."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.memory.database import Database
from app.memory.manager import MemoryManager
from app.tools.base import ToolError


@pytest.fixture()
def manager(tmp_path: Path) -> MemoryManager:
    db = Database(tmp_path / "x.db")
    db.initialize()
    return MemoryManager(db)


def test_edit_keeps_the_old_wording_and_refuses_secrets(manager: MemoryManager) -> None:
    memory, _ = manager.remember("my project folder is D:/work")
    changed = manager.edit(memory, "my project folder is E:/projects")
    assert changed.content == "my project folder is E:/projects" and changed.revision == memory.revision + 1
    assert manager.history(changed)[0]["content"] == "my project folder is D:/work"
    with pytest.raises(ToolError):
        manager.edit(changed, "my password is hunter2-Secret!99")
    with pytest.raises(ToolError):
        manager.edit(changed, "   ")


def test_import_validates_and_never_duplicates(manager: MemoryManager) -> None:
    manager.remember("I like dark mode")
    rows = [{"content": "I like dark mode", "category": "preferences"}, {"content": "My dog is called Bolt"},
            {"content": ""}, {"nope": 1}, "garbage", {"content": "x" * 5000}, {"content": "api key sk-abcdefghijklmnopqrstuvwxyz123456"}]
    counts = manager.import_rows(rows)
    assert counts == {"added": 1, "updated": 1, "skipped": 5}
    assert len([m for m in manager.all() if "dark mode" in m.content]) == 1
    with pytest.raises(ToolError):
        manager.import_rows({"not": "a list"})


def test_memories_used_for_an_answer_and_why(manager: MemoryManager, tmp_path: Path) -> None:
    from app.tools.memory import NoArgs, memory_used

    for i in range(30):
        manager.remember(f"fact number {i} about cooking")
    manager.remember("my laptop is a Lenovo Legion")
    section = manager.prompt_section("what laptop do I have")
    assert "Lenovo Legion" in section                                  # the matching one wins a place
    why = {text: reason for _, text, reason in manager.last_used}
    assert why["my laptop is a Lenovo Legion"] == "it matches words in your request"
    ctx = SimpleNamespace(get_memory=lambda: manager, database=manager._db)
    result = memory_used(NoArgs(), ctx)
    assert "Lenovo Legion" in result.message and "not something I just checked" in result.message


def test_import_through_the_tool(manager: MemoryManager, tmp_path: Path) -> None:
    from app.tools.memory import MemoryAdminArgs, memory_admin

    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "memories-20261018.json").write_text(json.dumps([{"content": "My sister lives in Sylhet"}]), "utf-8")
    ctx = SimpleNamespace(get_memory=lambda: manager, database=manager._db, paths=SimpleNamespace(data_dir=tmp_path),
                          clock=None)
    result = memory_admin(MemoryAdminArgs(action="import"), ctx)
    assert result.ok and result.data["added"] == 1
    with pytest.raises(ToolError):
        memory_admin(MemoryAdminArgs(action="import", file="missing.json"), ctx)
