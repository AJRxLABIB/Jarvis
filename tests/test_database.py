from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.memory.database import LATEST_SCHEMA_VERSION, MIGRATIONS, Database, StorageError, utc_now_iso

EXPECTED_TABLES = {"schema_migrations", "memories", "conversations", "automation_tasks", "settings_kv", "audit_log"}


def _tables(db: Database) -> set[str]:
    with db.connect() as conn:
        return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_initialize_creates_schema(tmp_path: Path) -> None:
    db = Database(tmp_path / "sub" / "assistant.db")
    assert db.initialize() == [m.version for m in MIGRATIONS]
    assert EXPECTED_TABLES <= _tables(db)
    assert db.schema_version() == LATEST_SCHEMA_VERSION
    assert db.integrity_check() == "ok"


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "assistant.db")
    db.initialize()
    assert db.initialize() == []


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "assistant.db")
    db.initialize()
    now = utc_now_iso()
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO memories (content, created_at, updated_at) VALUES (?, ?, ?)", ("temp", now, now)
            )
            raise RuntimeError("abort")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_constraints_enforced(tmp_path: Path) -> None:
    db = Database(tmp_path / "assistant.db")
    db.initialize()
    now = utc_now_iso()
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO memories (content, source, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("x", "llm", now, now),
            )


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    db = Database(tmp_path / "assistant.db")
    db.initialize()
    with db.connect() as conn:
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (LATEST_SCHEMA_VERSION + 5, "future", utc_now_iso()))
    with pytest.raises(StorageError, match="newer version"):
        db.initialize()


def test_corrupt_file_gives_storage_error(tmp_path: Path) -> None:
    path = tmp_path / "assistant.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(StorageError):
        Database(path).initialize()


def test_upgrade_keeps_data_and_writes_a_backup(tmp_path: Path, monkeypatch) -> None:
    import app.memory.database as database

    path = tmp_path / "assistant.db"
    monkeypatch.setattr(database, "MIGRATIONS", MIGRATIONS[:1])
    monkeypatch.setattr(database, "LATEST_SCHEMA_VERSION", 1)
    old = Database(path)
    old.initialize()
    now = utc_now_iso()
    with old.transaction() as conn:
        conn.execute("INSERT INTO memories (content, created_at, updated_at) VALUES ('kept', ?, ?)", (now, now))
    monkeypatch.setattr(database, "MIGRATIONS", MIGRATIONS)
    monkeypatch.setattr(database, "LATEST_SCHEMA_VERSION", MIGRATIONS[-1].version)

    new = Database(path)
    assert new.initialize() == [m.version for m in MIGRATIONS[1:]]
    with new.connect() as conn:
        row = conn.execute("SELECT content, value FROM memories").fetchone()
    assert (row["content"], row["value"]) == ("kept", None)
    assert (tmp_path / "assistant.v1.bak.db").is_file()
