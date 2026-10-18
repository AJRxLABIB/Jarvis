"""Safer updates: a verified data backup before the installer starts, a health check after, and a way back.

Flow (the installer itself is still the one in ``app/utils/updates.py``: https, SHA-256, code signature):

1. ``begin`` - just before the installer is started: copy the database with SQLite's backup API, verify the copy, and
   write ``data\\pending-update.json`` (from-version, to-version, where the backup is). If the backup cannot be made, the
   update is NOT started.
2. The installer replaces the program. Jarvis starts as the new version and migrates the database forward.
3. ``startup_check`` - on that first start: run the health checks (database integrity and schema, the diagnostics, the tool
   list builds). Healthy: the record is kept as history and the pending file is cleared. Unhealthy: the pending file stays
   and the problems are reported together with the way back.
4. ``rollback`` (``python run.py --rollback-update``, with Jarvis closed) puts the pre-update database back.

What "rollback" can and cannot do, honestly:

* It restores the DATA (database) to how it was just before the update. Anything saved after the update is lost; the
  database from just before the rollback is kept as ``pre-restore-*.db`` next to the backups.
* It does NOT reinstall the old PROGRAM. An older Jarvis cannot open a database that was migrated by a newer one, so
  after restoring you must install the previous version's installer yourself (from your releases page; Jarvis keeps no
  copy of old installers). Until you do, the newer program keeps working with the restored (older-schema) data.
* The installer's own file replacement is not something Jarvis can undo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.memory import backup
from app.memory.database import StorageError

PENDING = "pending-update.json"
HISTORY = "update-history.json"
KEEP_PRE_UPDATE_BACKUPS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def backup_folder(data_dir: Path) -> Path:
    return Path(data_dir) / "backups" / "pre-update"


def begin(paths: Any, from_version: str, to_version: str, installer_sha256: str = "") -> dict[str, Any]:
    """Make the pre-update backup and record the pending update. Raises StorageError (and records nothing) on failure."""
    saved = backup.create_backup(Path(paths.db_path), backup_folder(paths.data_dir), keep=KEEP_PRE_UPDATE_BACKUPS)
    record = {"from_version": from_version, "to_version": to_version, "started_at": _now(), "db_backup": str(saved),
              "installer_sha256": installer_sha256, "status": "installer_started"}
    _write(Path(paths.data_dir) / PENDING, record)
    return record


@dataclass
class UpdateCheck:
    state: str                    # not_installed | healthy | unhealthy | unknown_version
    from_version: str = ""
    to_version: str = ""
    problems: list[str] = field(default_factory=list)
    how_to_roll_back: str = ""

    def message(self) -> str:
        if self.state == "healthy":
            return f"Updated from {self.from_version} to {self.to_version}; the post-update health check passed."
        if self.state == "not_installed":
            return f"The update to {self.to_version} was not installed (still {self.from_version}); nothing changed."
        if self.state == "unknown_version":
            return (f"An update from {self.from_version} to {self.to_version} was started, but this is a different version. "
                    "Nothing was rolled back.")
        return (f"Updated to {self.to_version}, but the health check found problems: " + "; ".join(self.problems) + ". "
                + self.how_to_roll_back)


def health_problems(ctx: Any) -> list[str]:
    """Things that mean the new version is not healthy. Read-only; no network."""
    problems: list[str] = []
    try:
        from app.core import diagnostics

        for check in diagnostics.diagnose(ctx, include_network=False):
            if check.status == "fail":
                problems.append(f"{check.name}: {check.detail}")
    except Exception as exc:
        problems.append(f"the diagnostics could not run ({exc.__class__.__name__})")
    try:
        from app.tools.catalog import build_default_registry

        if len(build_default_registry()) < 10:
            problems.append("the tool list is almost empty")
    except Exception as exc:
        problems.append(f"the tool list could not be built ({exc.__class__.__name__})")
    return problems


def rollback_instructions(record: dict[str, Any]) -> str:
    return (f"To go back: close Jarvis, run `python run.py --rollback-update` (restores the database from just before the "
            f"update: {Path(record.get('db_backup', '')).name}), then install Jarvis {record.get('from_version', 'the previous version')} "
            "from your releases page - an older program cannot open the newer database, and Jarvis keeps no old installers.")


def startup_check(ctx: Any, current_version: str, problems: list[str] | None = None) -> UpdateCheck | None:
    """Call once at startup. None when no update is pending."""
    data_dir = Path(ctx.paths.data_dir)
    pending = _read(data_dir / PENDING, None)
    if not pending:
        return None
    frm, to = pending.get("from_version", ""), pending.get("to_version", "")
    if current_version == frm and frm != to:
        (data_dir / PENDING).unlink(missing_ok=True)
        return UpdateCheck("not_installed", frm, to)
    if current_version != to:
        return UpdateCheck("unknown_version", frm, to)
    found = health_problems(ctx) if problems is None else problems
    history = _read(data_dir / HISTORY, [])
    history.append({"from": frm, "to": to, "checked_at": _now(), "healthy": not found, "problems": found})
    _write(data_dir / HISTORY, history[-20:])
    if not found:
        (data_dir / PENDING).unlink(missing_ok=True)
        return UpdateCheck("healthy", frm, to)
    pending["status"] = "unhealthy"
    pending["problems"] = found
    _write(data_dir / PENDING, pending)
    return UpdateCheck("unhealthy", frm, to, found, rollback_instructions(pending))


def rollback(paths: Any) -> dict[str, Any]:
    """Restore the pre-update database. Jarvis must be closed (restore_backup refuses while it is open)."""
    pending = _read(Path(paths.data_dir) / PENDING, None)
    if not pending:
        raise StorageError("No update is pending, so there is nothing to roll back.")
    saved = Path(pending["db_backup"])
    kept = backup.restore_backup(Path(paths.db_path), saved, backup_folder(paths.data_dir))
    (Path(paths.data_dir) / PENDING).unlink(missing_ok=True)
    return {"restored_from": str(saved), "previous_database_kept_as": str(kept),
            "next_step": f"Install Jarvis {pending.get('from_version', 'the previous version')} yourself; "
                         "the program itself was not changed."}
