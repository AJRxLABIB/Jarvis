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
from dataclasses import dataclass, field
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


@dataclass
class RestoreTest:
    ok: bool
    backup: str
    schema_before: int = 0
    schema_after: int = 0
    tables: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def message(self) -> str:
        rows = sum(self.tables.values())
        if self.ok:
            upgraded = f", upgraded v{self.schema_before} -> v{self.schema_after}" if self.schema_after != self.schema_before else ""
            return (f"Restore test passed for {self.backup}: it opened in an isolated copy{upgraded}, passed its integrity "
                    f"check and accepted a write; {len(self.tables)} tables, {rows} rows ({self.seconds:.1f}s). "
                    "Your real database was not touched.")
        return f"Restore test FAILED for {self.backup}: " + "; ".join(self.problems)


def restore_test(backup: Path, scratch_parent: Path | None = None) -> RestoreTest:
    """Prove a backup can really be restored: copy it to a throw-away folder, open it the way Jarvis does (including any
    migrations), check integrity and foreign keys, count the rows, and make sure a write works. The live database and the
    backup file are never modified, and the scratch copy is always deleted."""
    import shutil
    import tempfile
    import time

    from app.memory.database import Database

    started = time.monotonic()
    report = RestoreTest(False, backup.name)
    scratch = Path(tempfile.mkdtemp(prefix="jarvis-restore-test-", dir=str(scratch_parent) if scratch_parent else None))
    try:
        if not backup.is_file():
            report.problems.append("the backup file does not exist")
            return report
        try:
            result, report.schema_before = _check(backup)
        except StorageError as exc:
            report.problems.append(str(exc))
            return report
        if result != "ok":
            report.problems.append(f"the file failed its integrity check ({result})")
            return report
        if report.schema_before > LATEST_SCHEMA_VERSION:
            report.problems.append(f"it is from a newer Jarvis (schema v{report.schema_before})")
            return report
        copy = scratch / "assistant.db"
        shutil.copyfile(backup, copy)
        try:
            db = Database(copy)
            db.initialize()                                   # exactly what a restored install does at its first start
            report.schema_after = int(db.schema_version())
            with db.connect() as conn:
                broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    report.problems.append(f"{len(broken)} rows point at records that do not exist")
                names = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                for name in names:
                    report.tables[name] = int(conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0])
            with db.transaction() as conn:                     # a write must work; then it is undone
                conn.execute("CREATE TEMP TABLE restore_probe (x INTEGER)")
                conn.execute("INSERT INTO restore_probe VALUES (1)")
        except Exception as exc:
            report.problems.append(f"opening the restored copy failed: {str(exc)[:160]}")
            return report
        report.ok = not report.problems
        return report
    finally:
        report.seconds = time.monotonic() - started
        shutil.rmtree(scratch, ignore_errors=True)


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
