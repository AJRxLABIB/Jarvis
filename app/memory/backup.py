"""Database backup and restore: verified snapshots, retention, and a restore that never destroys what's there.

* ``create_backup`` copies the live database with SQLite's backup API (safe while Jarvis runs, includes the WAL),
  checks the copy with ``PRAGMA quick_check`` and only then keeps it. Old snapshots beyond ``keep`` are pruned
  (migration backups ``*.vN.bak.db`` next to the database are not touched).
* ``restore_backup`` refuses a backup that is damaged or from a newer Jarvis, refuses while Jarvis is using the
  database, keeps the current database as ``pre-restore-<time>.db`` and swaps the file in atomically.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from app.memory.database import LATEST_SCHEMA_VERSION, StorageError
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("backup")
PREFIX = "assistant-"


def _check(path: Path) -> tuple[str, int]:
    """(quick_check result, schema version) of a database file; StorageError when it isn't a database."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = "; ".join(str(r[0]) for r in conn.execute("PRAGMA quick_check")) or "no result"
            has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'").fetchone()
            version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0] \
                if has_table else 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise StorageError(f"{path.name} is not a usable database: {exc}") from exc
    return result, int(version)


def list_backups(folder: Path) -> list[Path]:
    return sorted(folder.glob(f"{PREFIX}*.db"), reverse=True) if folder.is_dir() else []


def create_backup(db_path: Path, folder: Path, *, keep: int = 7, now: datetime | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = folder / f"{PREFIX}{stamp}.db"
    n = 1
    while target.exists():  # two backups in the same second
        n += 1
        target = folder / f"{PREFIX}{stamp}-{n}.db"
    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise StorageError(f"Could not back up the database: {exc}") from exc
    finally:
        source.close()
    result, version = _check(target)
    if result != "ok":
        target.unlink(missing_ok=True)
        raise StorageError(f"The backup failed its integrity check ({result}), so it was discarded.")
    for old in list_backups(folder)[max(keep, 1):]:
        old.unlink(missing_ok=True)
    log_event(_log, "db_backup", path=str(target), schema_version=version)
    return target


def restore_backup(db_path: Path, backup: Path, folder: Path) -> Path:
    """Replace the database with ``backup``. Returns where the previous database was kept."""
    if not backup.is_file():
        raise StorageError(f"There is no backup file {backup}.")
    result, version = _check(backup)
    if result != "ok":
        raise StorageError(f"That backup is damaged ({result}); the database was not changed.")
    if version > LATEST_SCHEMA_VERSION:
        raise StorageError(f"That backup is from a newer Jarvis (schema v{version}; this one supports up to "
                           f"v{LATEST_SCHEMA_VERSION}). Update the application first.")
    kept = folder / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    folder.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        lock = sqlite3.connect(str(db_path), timeout=1.0, isolation_level=None)
        try:
            lock.execute("BEGIN EXCLUSIVE")  # fails while Jarvis (or anything) is using the database
            lock.execute("ROLLBACK")
        except sqlite3.OperationalError:
            raise StorageError("Jarvis is using the database. Close it, then restore.") from None
        finally:
            lock.close()
        source = sqlite3.connect(str(db_path))
        try:
            dest = sqlite3.connect(str(kept))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
    staged = db_path.with_name(db_path.name + ".restoring")
    staged.write_bytes(backup.read_bytes())
    for suffix in ("-wal", "-shm"):  # the old database's journal must not be replayed onto the restored file
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    os.replace(staged, db_path)
    log_event(_log, "db_restored", backup=str(backup), previous=str(kept), schema_version=version)
    return kept
