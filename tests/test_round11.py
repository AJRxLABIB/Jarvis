from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from app.ai.health import ModelHealth
from app.core import metrics
from app.core.upkeep import Upkeep
from app.memory import backup

from app.memory.database import LATEST_SCHEMA_VERSION, Database
from app.memory.settings_store import SettingsStore


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "assistant.db")
    db.initialize()
    with db.transaction() as conn:
        conn.execute("INSERT INTO memories (content, created_at, updated_at) VALUES ('keep me', 'x', 'x')")
    return db


def test_restore_test_proves_a_backup_opens_and_leaves_everything_untouched(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    saved = backup.create_backup(db.path if hasattr(db, "path") else tmp_path / "assistant.db", tmp_path / "backups")
    before = saved.read_bytes()
    result = backup.restore_test(saved)
    assert result.ok and result.tables["memories"] == 1 and result.schema_after == result.schema_before
    assert "Your real database was not touched" in result.message()
    assert saved.read_bytes() == before                                   # the backup itself was not modified
    assert not [p for p in Path(saved.parent).iterdir() if p.name.startswith("jarvis-restore-test")]


def test_restore_test_cleans_up_its_scratch_copy(tmp_path: Path) -> None:
    import tempfile

    db = make_db(tmp_path)
    saved = backup.create_backup(tmp_path / "assistant.db", tmp_path / "backups")
    before = {p.name for p in Path(tempfile.gettempdir()).glob("jarvis-restore-test-*")}
    backup.restore_test(saved)
    assert {p.name for p in Path(tempfile.gettempdir()).glob("jarvis-restore-test-*")} == before
    assert db.schema_version() > 0


def test_restore_test_upgrades_an_older_backup_the_way_a_real_restore_would(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    saved = backup.create_backup(tmp_path / "assistant.db", tmp_path / "backups")
    old = tmp_path / "backups" / "assistant-20200101-000000.db"
    conn = sqlite3.connect(str(saved))
    dest = sqlite3.connect(str(old))
    conn.backup(dest)
    conn.close()
    dest.execute("DROP TRIGGER IF EXISTS sync_todos_insert")           # pretend v30 never ran
    dest.execute("DROP TRIGGER IF EXISTS sync_todos_update")
    dest.execute("DROP TRIGGER IF EXISTS sync_todos_delete")
    dest.execute("DROP TABLE sync_log")
    dest.execute("DROP TABLE sync_ops")
    dest.execute("DROP TABLE sync_prefs")
    dest.execute("DROP TABLE push_tokens")                                  # ... nor v31
    dest.execute("DROP TABLE clipboard_items")                              # ... nor v32 (add later tables here too)
    dest.execute("DROP TABLE expenses")                                     # ... nor v33
    dest.execute("DROP TABLE budgets")
    for table in ("phone_commands", "phone_state", "journal", "habit_checks", "habit_defs", "learning_turns",
                  "growth_items"):            # ... nor v34
        dest.execute(f"DROP TABLE {table}")
    dest.execute("DELETE FROM schema_migrations WHERE version >= 30")
    dest.commit()
    dest.close()
    result = backup.restore_test(old)
    assert result.ok and result.schema_before == 29 and result.schema_after == LATEST_SCHEMA_VERSION and f"upgraded v29 -> v{LATEST_SCHEMA_VERSION}" in result.message()
    assert db.schema_version() == LATEST_SCHEMA_VERSION                                   # the live database was not changed


def test_restore_test_fails_loudly_for_damaged_missing_and_newer_backups(tmp_path: Path) -> None:
    bad = tmp_path / "assistant-bad.db"
    bad.write_bytes(b"this is not a database at all")
    result = backup.restore_test(bad)
    assert not result.ok and "FAILED" in result.message()
    assert not backup.restore_test(tmp_path / "nope.db").ok
    db = make_db(tmp_path)
    saved = backup.create_backup(tmp_path / "assistant.db", tmp_path / "backups")
    c = sqlite3.connect(str(saved))
    c.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (999, 'future', 'x')")
    c.commit()
    c.close()
    newer = backup.restore_test(saved)
    assert not newer.ok and "newer Jarvis" in newer.message()
    assert db.schema_version() == LATEST_SCHEMA_VERSION


def test_the_daily_backup_is_restore_tested_and_a_failure_is_recorded(tmp_path: Path, monkeypatch) -> None:
    db = make_db(tmp_path)
    up = Upkeep(db, tmp_path / "assistant.db", tmp_path / "backups", sampler=lambda: {})
    did = up.run_once()
    assert did["backed_up"]
    last = SettingsStore(db).get("backup_restore_test")
    assert last["ok"] is True and "passed" in last["message"]

    monkeypatch.setattr(backup, "restore_test", lambda p: backup.RestoreTest(False, p.name, problems=["boom"]))
    up2 = Upkeep(db, tmp_path / "assistant.db", tmp_path / "backups2", sampler=lambda: {})
    up2.run_once()
    assert SettingsStore(db).get("backup_restore_test")["ok"] is False and "restore test" in up2.last_error


def test_a_failed_restore_test_shows_up_in_troubleshooting(tmp_path: Path) -> None:
    from app.config.settings import Settings
    from app.core import troubleshoot
    from app.tools.base import ToolContext

    db = make_db(tmp_path)
    SettingsStore(db).set("backup_restore_test", {"at": "x", "ok": False, "backup": "b.db", "message": "FAILED: boom"})
    ctx = ToolContext(Settings(ai_provider="ollama"), database=db)
    findings = troubleshoot.scan(ctx) if hasattr(troubleshoot, "scan") else []
    assert any(f.problem.startswith("Your latest backup could not be restored") for f in findings)


def test_model_health_tracks_first_token_and_tool_call_reliability() -> None:
    h = ModelHealth()
    h.success("m", 2.0)
    h.first_token("m", 0.4)
    h.first_token("m", 0.6)
    for ok in (True, True, True, False):
        h.tool_call("m", ok)
    info = h.summary()["m"]
    assert info["first_token_seconds"] == 0.5 and info["tool_calls"] == 4 and info["tool_call_ok_rate"] == 0.75
    assert ModelHealth().summary() == {}


def test_a_model_that_keeps_calling_tools_wrongly_is_flagged_but_a_small_sample_is_not(tmp_path: Path) -> None:
    db = make_db(tmp_path)

    def flagged(health: ModelHealth) -> bool:
        found = metrics.analyse_state(db, datetime.now().astimezone(), health.summary())
        return any(f.kind == "model_tool_calls" for f in found)

    h = ModelHealth()
    for _ in range(3):
        h.tool_call("tiny", False)
    assert not flagged(h)                                              # three calls is not evidence
    for _ in range(3):
        h.tool_call("tiny", False)
    assert flagged(h)
    good = ModelHealth()
    for _ in range(10):
        good.tool_call("big", True)
    assert not flagged(good)
