from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.memory.database import Database
from app.memory.manager import MemoryManager
from app.tools.base import ToolContext, ToolError
from app.tools.memory import MemoryAdminArgs, memory_admin


@pytest.fixture()
def manager(tmp_path: Path) -> MemoryManager:
    db = Database(tmp_path / "m.db")
    db.initialize()
    m = MemoryManager(db)
    m.remember("I prefer dark mode", category="preference")
    m.remember("My code editor is VS Code", category="preference")
    m.remember("The tender deadline is 30 October", category="project")
    return m


def ctx_for(manager: MemoryManager, tmp_path: Path) -> ToolContext:
    ctx = ToolContext(Settings(), database=manager._db)
    ctx.get_memory = lambda: manager
    ctx.paths = SimpleNamespace(data_dir=tmp_path)
    return ctx


def test_categories_disable_enable_and_forget(manager: MemoryManager) -> None:
    assert manager.categories()["preference"] == {"total": 2, "active": 2}
    assert manager.set_category_disabled("preference", True) == 2
    assert manager.categories()["preference"]["active"] == 0 and len(manager.all()) == 1  # not used any more
    assert manager.set_category_disabled("preference", False) == 2
    assert manager.forget_category("project") == 1 and "project" not in manager.categories()
    assert manager.forget_category("nothing") == 0


def test_export_leaves_private_out_unless_asked(manager: MemoryManager) -> None:
    private = manager.find_one("tender deadline")
    manager.set_private(private, True)
    rows = manager.export()
    assert len(rows) == 2 and all("tender" not in r["content"] for r in rows)
    assert len(manager.export(include_private=True)) == 3
    assert {"category", "content", "source", "stated_by_you", "confidence"} <= set(rows[0])


def test_event_retention_only_touches_what_jarvis_recorded(manager: MemoryManager) -> None:
    manager.record_event("Mission 'tidy desktop' finished")
    later = datetime.now(timezone.utc) + timedelta(days=40)
    assert manager.forget_events_older_than(0, later) == 0  # 0 keeps everything
    assert manager.forget_events_older_than(30, datetime.now(timezone.utc)) == 0  # too recent
    assert manager.forget_events_older_than(30, later) == 1
    assert len(manager.all()) == 3  # what you told it is never touched


def test_the_tool_exports_a_file_and_validates(manager: MemoryManager, tmp_path: Path) -> None:
    ctx = ctx_for(manager, tmp_path)
    result = memory_admin(MemoryAdminArgs(action="export"), ctx)
    data = json.loads(Path(result.data["path"]).read_text(encoding="utf-8"))
    assert result.data["count"] == 3 == len(data)
    assert "preference" in memory_admin(MemoryAdminArgs(action="categories"), ctx).data["categories"]
    with pytest.raises(ToolError):
        memory_admin(MemoryAdminArgs(action="forget_category", category="nope"), ctx)
    assert "Deleted 1" in memory_admin(MemoryAdminArgs(action="forget_category", category="project"), ctx).message
