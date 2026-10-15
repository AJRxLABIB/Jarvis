"""Database backup and restore."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.memory import backup
from app.memory.database import LATEST_SCHEMA_VERSION, Database, StorageError


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "assistant.db")
    database.initialize()
    with database.transaction() as conn:
        conn.execute("INSERT INTO todos (text, created_at) VALUES ('first', 'now')")
    return database


def todos(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT text FROM todos ORDER BY id")]
    finally:
        conn.close()


def test_backup_is_a_verified_copy_and_old_ones_are_pruned(db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    start = datetime(2026, 10, 1, 12, 0, 0)
    made = [backup.create_backup(db.path, folder, keep=3, now=start + timedelta(minutes=i)) for i in range(5)]
    assert todos(made[-1]) == ["first"]
    kept = backup.list_backups(folder)
    assert len(kept) == 3 and made[0] not in kept and made[-1] in kept  # the newest three
    same_second = backup.create_backup(db.path, folder, keep=9, now=start + timedelta(minutes=4))
    assert same_second not in made  # never overwrites a snapshot


def test_restore_brings_back_the_old_data_and_keeps_the_current_one(db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    snapshot = backup.create_backup(db.path, folder)
    with db.transaction() as conn:
        conn.execute("INSERT INTO todos (text, created_at) VALUES ('second', 'now')")
    assert todos(db.path) == ["first", "second"]
    kept = backup.restore_backup(db.path, snapshot, folder)
    assert todos(db.path) == ["first"] and todos(kept) == ["first", "second"]  # nothing was destroyed
    assert Database(db.path).integrity_check() == "ok"


def test_restore_refuses_bad_backups(db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    with pytest.raises(StorageError, match="no backup file"):
        backup.restore_backup(db.path, tmp_path / "missing.db", folder)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database at all" * 10)
    with pytest.raises(StorageError):
        backup.restore_backup(db.path, junk, folder)
    newer = tmp_path / "newer.db"
    conn = sqlite3.connect(str(newer))
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
    conn.execute("INSERT INTO schema_migrations VALUES (?, 'future', 'x')", (LATEST_SCHEMA_VERSION + 3,))
    conn.commit()
    conn.close()
    with pytest.raises(StorageError, match="newer Jarvis"):
        backup.restore_backup(db.path, newer, folder)
    assert todos(db.path) == ["first"]  # untouched by every refusal


def test_restore_refuses_while_the_database_is_in_use(db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    snapshot = backup.create_backup(db.path, folder)
    holder = sqlite3.connect(str(db.path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # something is writing
    try:
        with pytest.raises(StorageError, match="is using the database"):
            backup.restore_backup(db.path, snapshot, folder)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert todos(db.path) == ["first"]
