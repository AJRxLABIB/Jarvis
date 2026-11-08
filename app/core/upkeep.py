"""Quiet housekeeping that runs inside the app's own loop: the health history and the daily database backup.

``Upkeep.run`` is called by the scheduler loop (every 30 s). It does nothing most of the time: a reading is taken
at most once a minute and a backup at most once a day. Slow work (psutil, copying the database) runs in a worker
thread, so neither the window nor the loop ever waits for it, and a failure is logged and never stops the app.
"""

from __future__ import annotations

import time

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
                 sampler: Callable[[], dict] | None = None, clock: Callable[[], datetime] | None = None,
                 process_sampler: Callable[[], list] | None = None, config_dir: Path | None = None) -> None:
        self.database, self.db_path, self.backup_folder = database, db_path, backup_folder
        self.config_dir = config_dir
        self.process_sampler = process_sampler
        self.settings = settings
        self.sampler = sampler or (lambda: metrics.sample_now(str(db_path.anchor or "C:\\")))
        self.clock = clock or datetime.now
        self.store = metrics.MetricsStore(database, timedelta(days=int(getattr(settings, "metrics_retention_days", 14) or 14)))
        self.last_error = ""

    def _on(self, name: str) -> bool:
        return bool(getattr(self.settings, name, True)) if self.settings is not None else True

    def _defer(self, now: datetime) -> bool:
        """A busy PC or a low battery postpones the daily backup - but never for more than three days: a backup must still happen."""
        try:
            from app.core import attention, perf

            plan = perf.current_plan(self.settings)
            if attention.allow("background", plan=plan)[0]:       # the current mode (gaming, presentation, power saving) and the PC load
                return False
        except Exception:
            return False
        existing = backup.list_backups(self.backup_folder)
        if not existing:
            return False
        try:
            last = datetime.strptime(existing[0].stem.removeprefix(backup.PREFIX)[:15], "%Y%m%d-%H%M%S")
        except ValueError:
            return False
        return now - last < timedelta(days=3)

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
                if did["recorded"] and bool(getattr(self.settings, "process_history", False)):
                    from app.core import procstats

                    procstats.ProcessStore(self.database, self.store.retention).record(
                        self.process_sampler() if self.process_sampler else procstats.sample_top())
            except Exception as exc:
                self.last_error = f"metrics: {exc}"
                _log.warning("could not record the health history", exc_info=True)
        if self._on("auto_backup") and self.backup_due(now) and not self._defer(now):
            try:
                path = backup.create_backup(self.db_path, self.backup_folder, now=now)
                did["backed_up"] = True
                log_event(_log, "auto_backup", path=str(path))
                self._test_restore(path, now)
            except Exception as exc:
                self.last_error = f"backup: {exc}"
                _log.warning("the automatic backup failed", exc_info=True)
        if bool(getattr(self.settings, "cyber_guardian", False)) and self._guardian_due(now):
            try:
                from app.core import guardian

                found = guardian.instance(self.settings, self.database, self.backup_folder).scan()
                self._last_guardian = now
                did["guardian"] = len(found)
                if found:
                    guardian.notify(self.database, found)
            except Exception:
                _log.warning("the guardian scan failed", exc_info=True)
        try:
            from app.core import learning_quality
            from app.memory.settings_store import SettingsStore

            found = learning_quality.scheduled(self.database, SettingsStore(self.database), now)
            if found:                                   # a decision for you: nothing is retired without asking
                did["learning_audit"] = True
                log_event(_log, "learning_audit_needs_review", count=len(found["retire"]), text=learning_quality.describe(found))
        except Exception:
            _log.debug("learning audit failed", exc_info=True)
        try:
            from app.core import resources

            resources.apply_mode_hold(attention.allow("background")[0], attention.allow("heavy")[0])
        except Exception:
            _log.debug("scheduler hold failed", exc_info=True)
        if self.config_dir is not None and attention.allow("background")[0]:
            try:
                from app.core import sensor_rules
                from app.memory.settings_store import SettingsStore

                if sensor_rules.due(self._last_sensors) and (Path(self.config_dir) / sensor_rules.FILE).exists():
                    self._last_sensors = time.monotonic()
                    if sensor_rules.run(self.database, SettingsStore(self.database), self.config_dir):
                        did["sensor_alerts"] = True
            except Exception:
                _log.debug("sensor rules failed", exc_info=True)
        try:
            from app.tools import transaction_tool

            if transaction_tool.reconcile_all(self.database):
                did["transactions"] = True
        except Exception:
            _log.debug("transaction reconcile failed", exc_info=True)
        days = int(getattr(self.settings, "memory_event_retention_days", 0) or 0)
        if days > 0 and self._retention_due(now):  # at most once an hour
            try:
                from app.memory.manager import MemoryManager

                removed = MemoryManager(self.database).forget_events_older_than(days)
                self._last_retention = now
                did["retention"] = True
                if removed:
                    log_event(_log, "memory_retention", removed=removed, days=days)
            except Exception:
                _log.warning("memory retention failed", exc_info=True)
        return did

    _last_retention: datetime | None = None
    _last_guardian: datetime | None = None
    _last_sensors: float | None = None

    def _guardian_due(self, now: datetime) -> bool:
        return self._last_guardian is None or now - self._last_guardian >= timedelta(minutes=3)

    def _test_restore(self, path: Path, now: datetime) -> None:
        """A backup is only trusted once it was restored somewhere harmless: do that for every new daily backup."""
        from app.memory.settings_store import SettingsStore

        try:
            result = backup.restore_test(path)
            SettingsStore(self.database).set("backup_restore_test", {
                "at": now.isoformat(timespec="seconds"), "ok": result.ok, "backup": result.backup,
                "message": result.message()[:400]})
            if result.ok:
                log_event(_log, "restore_test_passed", backup=result.backup, tables=len(result.tables))
            else:
                self.last_error = f"restore test: {'; '.join(result.problems)[:200]}"
                _log.warning("the restore test of %s failed: %s", result.backup, result.problems)
        except Exception:
            _log.warning("the restore test could not run", exc_info=True)

    def _retention_due(self, now: datetime) -> bool:
        return self._last_retention is None or now - self._last_retention >= timedelta(hours=1)

    async def watch_research(self, fetcher=None) -> list[str]:
        """Re-read the saved sources of ONE watched research project that is due (opt-in per project).

        Only addresses already saved as sources are fetched (public pages only). Claims whose page changed are marked
        'needs recheck'; nothing else is touched. Returns the names of projects that had changes.
        """
        from app.core import perf
        from app.core.research_store import ResearchStore

        try:
            if perf.current_plan(self.settings).defer_background:
                return []                                     # watched research waits while the PC is busy / on a low battery
        except Exception:
            pass
        store = ResearchStore(self.database)
        due = await asyncio.to_thread(store.due)
        if not due:
            return []
        name = due[0]
        if fetcher is None:
            from app.browser import pages

            async def fetcher(url: str) -> str:  # noqa: E306
                return (await pages.fetch(url))[2]
        fetched: dict[str, str | None] = {}
        for source in await asyncio.to_thread(lambda: store.sources(store.project(name)["id"])):
            try:
                fetched[source["url"]] = await fetcher(source["url"])
            except Exception:
                fetched[source["url"]] = None
        report = await asyncio.to_thread(store.refresh, name, fetched)
        await asyncio.to_thread(store.mark_checked, name)
        if report["changed"]:
            log_event(_log, "research_source_changed", project=name, changed=len(report["changed"]))
        return [name] if report["changed"] else []

    async def run(self) -> dict[str, bool]:
        did = await asyncio.to_thread(self.run_once)
        await self.push_pings()
        try:   # clipboard memory: items past their time limit are removed even when the feature was switched off since
            from app.core.clipboard_memory import ClipboardStore

            await asyncio.to_thread(ClipboardStore(self.database, int(getattr(self.settings, "clipboard_retention_days", 7) or 7)).purge_expired)
        except Exception:
            _log.debug("clipboard purge failed", exc_info=True)
        try:
            from app.core import attention

            if self._research_check_due() and attention.allow("network")[0] and attention.allow("background")[0]:
                await self.watch_research()
        except Exception:
            _log.warning("research monitoring failed", exc_info=True)
        return did

    _fcm = None

    async def push_pings(self) -> dict[str, int]:
        """FCM_SERVICE_ACCOUNT set: send content-free wake-up pings for new notification events (app/remote/push.py)."""
        path = getattr(self.settings, "fcm_service_account", None)
        if not path:
            return {}
        try:
            from app.core import emergency
            from app.remote import push

            if self._fcm is None:
                self._fcm = push.FcmClient(path)
            from app.core import proactive

            def quiet() -> bool:
                return proactive.in_quiet_hours(self.clock(), proactive.parse_quiet_hours(getattr(self.settings, "quiet_hours", "")))

            return await asyncio.to_thread(push.dispatch, self.database, self._fcm,
                                           emergency_on=bool(emergency.STATE.active()), quiet=quiet)
        except Exception as exc:
            if str(exc) != self.last_error:
                _log.warning("push pings failed: %s", exc.__class__.__name__)
            self.last_error = str(exc)
            return {}

    _last_research: datetime | None = None

    def _research_check_due(self) -> bool:
        now = self.clock()
        if self._last_research is None or now - self._last_research >= timedelta(minutes=30):
            self._last_research = now
            return True
        return False
