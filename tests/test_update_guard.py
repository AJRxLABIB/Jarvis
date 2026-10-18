from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.core import update_guard as ug
from app.memory.database import Database, StorageError
from app.utils.paths import AppPaths


def make(tmp_path: Path):
    paths = AppPaths.build(tmp_path)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(paths.db_path)
    db.initialize()
    with db.transaction() as conn:
        conn.execute("INSERT INTO memories (content, created_at, updated_at) VALUES ('before the update', 'x', 'x')")
    return SimpleNamespace(paths=paths, database=db, settings=None), paths, db


def memories(db: Database) -> list[str]:
    with db.connect() as conn:
        return [r[0] for r in conn.execute("SELECT content FROM memories ORDER BY id")]


def test_begin_makes_a_verified_backup_and_records_the_pending_update(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    record = ug.begin(paths, "3.25.0", "3.26.0", "a" * 64)
    saved = Path(record["db_backup"])
    assert saved.is_file() and saved.parent == ug.backup_folder(paths.data_dir)
    conn = sqlite3.connect(str(saved))
    assert conn.execute("SELECT content FROM memories").fetchone()[0] == "before the update"
    conn.close()
    pending = json.loads((paths.data_dir / ug.PENDING).read_text())
    assert pending["from_version"] == "3.25.0" and pending["to_version"] == "3.26.0" and pending["status"] == "installer_started"


def test_a_failed_backup_records_nothing_so_the_update_is_not_started(tmp_path: Path) -> None:
    paths = AppPaths.build(tmp_path)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.db_path.write_bytes(b"this is not a database")
    with pytest.raises(StorageError):
        ug.begin(paths, "3.25.0", "3.26.0")
    assert not (paths.data_dir / ug.PENDING).exists()


def test_old_pre_update_backups_are_pruned(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    for _ in range(6):
        ug.begin(paths, "1", "2")
    assert len(list(ug.backup_folder(paths.data_dir).glob("*.db"))) == ug.KEEP_PRE_UPDATE_BACKUPS


def test_nothing_pending_means_no_check(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    assert ug.startup_check(ctx, "3.26.0", problems=[]) is None


def test_update_that_was_not_installed_clears_the_pending_record(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    ug.begin(paths, "3.25.0", "3.26.0")
    result = ug.startup_check(ctx, "3.25.0", problems=[])
    assert result.state == "not_installed" and "nothing changed" in result.message()
    assert not (paths.data_dir / ug.PENDING).exists()


def test_healthy_update_is_recorded_and_cleared(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    ug.begin(paths, "3.25.0", "3.26.0")
    result = ug.startup_check(ctx, "3.26.0", problems=[])
    assert result.state == "healthy" and "health check passed" in result.message()
    assert not (paths.data_dir / ug.PENDING).exists()
    history = json.loads((paths.data_dir / ug.HISTORY).read_text())
    assert history[-1]["healthy"] is True and history[-1]["to"] == "3.26.0"


def test_unhealthy_update_keeps_the_pending_record_and_explains_the_way_back(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    ug.begin(paths, "3.25.0", "3.26.0")
    result = ug.startup_check(ctx, "3.26.0", problems=["Database: integrity check failed"])
    assert result.state == "unhealthy" and "--rollback-update" in result.how_to_roll_back
    assert "3.25.0" in result.how_to_roll_back and "keeps no old installers" in result.how_to_roll_back
    pending = json.loads((paths.data_dir / ug.PENDING).read_text())
    assert pending["status"] == "unhealthy" and pending["problems"] == ["Database: integrity check failed"]


def test_an_unexpected_version_changes_nothing(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    ug.begin(paths, "3.25.0", "3.26.0")
    assert ug.startup_check(ctx, "9.9.9", problems=[]).state == "unknown_version"
    assert (paths.data_dir / ug.PENDING).exists()


def test_the_real_health_check_passes_on_a_good_install(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    ctx.settings = Settings()
    assert ug.health_problems(ctx) == []


def test_real_health_check_reports_a_damaged_database(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    ctx.settings = Settings()
    ctx.database = SimpleNamespace(connect=db.connect, schema_version=lambda: 3)    # an old schema behind the new program
    assert ug.health_problems(ctx) == []                                           # warn-level only: not a failure
    broken = SimpleNamespace(connect=lambda: (_ for _ in ()).throw(sqlite3.DatabaseError("malformed")),
                             schema_version=lambda: 0)
    ctx.database = broken
    assert any("Database" in p for p in ug.health_problems(ctx))


def test_rollback_restores_the_data_keeps_the_newer_copy_and_never_touches_the_program(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    ug.begin(paths, "3.25.0", "3.26.0")
    with db.transaction() as conn:   # the new version writes more data after the update
        conn.execute("INSERT INTO memories (content, created_at, updated_at) VALUES ('after the update', 'x', 'x')")
    assert memories(db) == ["before the update", "after the update"]
    done = ug.rollback(paths)
    assert memories(Database(paths.db_path)) == ["before the update"]
    kept = Path(done["previous_database_kept_as"])
    assert kept.is_file()                                                          # the newer data is not destroyed
    assert "Install Jarvis 3.25.0 yourself" in done["next_step"]
    assert not (paths.data_dir / ug.PENDING).exists()


def test_rollback_without_a_pending_update_is_refused(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    with pytest.raises(StorageError, match="nothing to roll back"):
        ug.rollback(paths)


def test_rollback_refuses_a_missing_backup(tmp_path: Path) -> None:
    ctx, paths, db = make(tmp_path)
    record = ug.begin(paths, "3.25.0", "3.26.0")
    Path(record["db_backup"]).unlink()
    with pytest.raises(StorageError):
        ug.rollback(paths)
    assert memories(db) == ["before the update"]                                   # nothing was changed
