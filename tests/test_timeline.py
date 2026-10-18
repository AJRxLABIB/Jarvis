from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.core import timeline
from app.core.runs import Run, RunStore, running
from app.memory.audit import AuditLog
from app.memory.database import Database
from app.tools.base import ToolContext
from app.tools.diagnostics_tools import TimelineArgs, activity_timeline


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    database.initialize()
    audit, store = AuditLog(database), RunStore(database)
    with running(Run("voice", "open notepad and write hello", id="r1"), store):
        audit.record("tool_result", tool="open_application", level=1, details={"ok": True, "message": "Opened Notepad"})
        audit.record("verification", tool="open_application", details={"ok": True})
        audit.record("tool_result", tool="write_document", level=1, details={"ok": True, "message": "Saved hello.txt"})
    with running(Run("automation", "nightly cleanup", id="r2"), store):
        audit.record("tool_result", tool="organize_desktop", level=1, details={"ok": False, "error": "blocked"})
        audit.record("tool_result", tool="get_datetime", level=0, details={"ok": True, "message": "9:00"})
    return database


def test_entries_show_actor_and_verification(db: Database) -> None:
    items = {e["tool"]: e for e in timeline.entries(db)}
    assert items["open_application"]["actor"] == "you (voice)" and items["open_application"]["verified"] is True
    assert items["write_document"]["verified"] is None
    assert items["organize_desktop"]["actor"] == "a scheduled automation" and not items["organize_desktop"]["ok"]
    assert "notepad" in items["open_application"]["request"]


def test_filters_and_unverified(db: Database) -> None:
    assert [e["tool"] for e in timeline.entries(db, query="notepad")] == ["write_document", "open_application"]
    assert [e["tool"] for e in timeline.entries(db, actor="automation")] == ["get_datetime", "organize_desktop"]
    assert [e["tool"] for e in timeline.unverified(db)] == ["write_document"]  # safe and failed ones are not listed
    assert "not verified" in timeline.describe(timeline.entries(db)) and timeline.describe([]) == "No matching activity."


def test_secrets_are_not_shown(db: Database) -> None:
    AuditLog(db).record("tool_result", tool="x", level=1, details={"ok": True, "message": "token=abcd1234efgh5678ijkl"})
    shown = json.dumps(timeline.entries(db, limit=1))
    assert "abcd1234efgh5678ijkl" not in shown


def test_export_and_delete(db: Database, tmp_path: Path) -> None:
    path, count = timeline.export(db, tmp_path / "out")
    assert count == 4 and len(json.loads(path.read_text(encoding="utf-8"))) == 4
    with db.transaction() as conn:
        conn.execute("UPDATE audit_log SET ts = ? WHERE tool = 'get_datetime'",
                     ((datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds"),))
    assert timeline.delete_older_than(db, 30) == 1
    assert "get_datetime" not in {e["tool"] for e in timeline.entries(db)}


def test_the_tool(db: Database, tmp_path: Path) -> None:
    ctx = ToolContext(Settings(), database=db)
    ctx.paths = SimpleNamespace(data_dir=tmp_path)
    assert "never confirmed" in activity_timeline(TimelineArgs(action="unverified"), ctx).message
    assert activity_timeline(TimelineArgs(action="search", query="notepad"), ctx).data["count"] == 2
    assert activity_timeline(TimelineArgs(action="export"), ctx).data["count"] == 4
    assert activity_timeline(TimelineArgs(action="delete_older", days=365), ctx).data["removed"] == 0
