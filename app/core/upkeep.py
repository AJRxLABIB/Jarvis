"""Quiet housekeeping that runs inside the app's own loop: the health history and the daily database backup.

``Upkeep.run`` is called by the scheduler loop (every 30 s). It does nothing most of the time: a reading is taken
at most once a minute and a backup at most once a day. Slow work (psutil, copying the database) runs in a worker
thread, so neither the window nor the loop ever waits for it, and a failure is logged and never stops the app.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from app.core import metrics
from app.memory import backup
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("upkeep")
BACKUP_EVERY = timedelta(hours=24)


class Upkeep:
    def __init__(self, database, db_path: Path, backup_folder: Path, *, settings=None,
                 sampler: Callable[[], dict] | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.database, self.db_path, self.backup_folder = database, db_path, backup_folder
        self.settings = settings
        self.sampler = sampler or (lambda: metrics.sample_now(str(db_path.anchor or "C:\\")))
        self.clock = clock or datetime.now
        self.store = metrics.MetricsStore(database)
        self.last_error = ""

    def _on(self, name: str) -> bool:
        return bool(getattr(self.settings, name, True)) if self.settings is not None else True

    def backup_due(self, now: datetime) -> bool:
        existing = backup.list_backups(self.backup_folder)
        if not existing:
            return True
        try:
            stamp = existing[0].stem.removeprefix(backup.PREFIX)[:15]
            last = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        except ValueError:
            return True
        return now - last >= BACKUP_EVERY

    def run_once(self) -> dict[str, bool]:
        """Blocking version (used in a worker thread and in tests). Returns what it did."""
        did = {"recorded": False, "backed_up": False}
        now = self.clock()
        if self._on("metrics_history"):
            try:
                did["recorded"] = self.store.record(self.sampler())
            except Exception as exc:
                self.last_error = f"metrics: {exc}"
                _log.warning("could not record the health history", exc_info=True)
        if self._on("auto_backup") and self.backup_due(now):
            try:
                path = backup.create_backup(self.db_path, self.backup_folder, now=now)
                did["backed_up"] = True
                log_event(_log, "auto_backup", path=str(path))
            except Exception as exc:
                self.last_error = f"backup: {exc}"
                _log.warning("the automatic backup failed", exc_info=True)
        return did

    async def run(self) -> dict[str, bool]:
        return await asyncio.to_thread(self.run_once)
