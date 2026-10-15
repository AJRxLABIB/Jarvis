"""Diagnostics and recovery: quick checks of Jarvis's own health, and safe repairs for what they find.

diagnose() runs local, read-only checks (no network except a 2-second look at the local Ollama) and returns one
line per check: ok / warn / fail, what it found, and what to do. repair() fixes only Jarvis's own bookkeeping,
never your files: it closes requests left "running" by a crash, pauses missions stuck mid-step, expires idle phone
sessions and removes out-of-date Python cache files. Everything it changes is reported.
"""

from __future__ import annotations

import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STUCK_MINUTES = 30
LOW_DISK_BYTES = 1_000_000_000
CRITICAL_DISK_BYTES = 200_000_000
ERROR_WARN = 20


@dataclass
class Check:
    name: str
    status: str        # ok / warn / fail
    detail: str
    fix: str = ""

    def line(self) -> str:
        mark = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}[self.status]
        return f"[{mark}] {self.name}: {self.detail}" + (f" -> {self.fix}" if self.fix else "")


def _cutoff(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def check_database(db) -> Check:
    from app.memory.database import LATEST_SCHEMA_VERSION

    try:
        with db.connect() as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
        version = db.schema_version()
    except Exception as exc:
        return Check("Database", "fail", f"could not be read ({exc.__class__.__name__})",
                     "restore the newest backup in the data folder")
    if result != "ok":
        return Check("Database", "fail", f"integrity check: {str(result)[:120]}", "restore the newest backup")
    if version != LATEST_SCHEMA_VERSION:
        return Check("Database", "warn", f"schema {version}, expected {LATEST_SCHEMA_VERSION}", "restart Jarvis")
    return Check("Database", "ok", f"intact, schema {version}")


def check_disk(folder: Path) -> Check:
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return Check("Disk space", "warn", "could not be measured")
    gb = free / 1e9
    if free < CRITICAL_DISK_BYTES:
        return Check("Disk space", "fail", f"only {gb:.2f} GB free", "free some space; Jarvis can't save safely")
    if free < LOW_DISK_BYTES:
        return Check("Disk space", "warn", f"{gb:.2f} GB free", "free some space soon")
    return Check("Disk space", "ok", f"{gb:.1f} GB free")


def check_stuck(db, current_run: str | None) -> Check:
    cutoff = _cutoff(STUCK_MINUTES)
    try:
        with db.connect() as conn:
            runs = conn.execute("SELECT COUNT(*) FROM agent_runs WHERE state = 'running' AND started_at < ? "
                                "AND id != ?", (cutoff, current_run or "")).fetchone()[0]
            missions = conn.execute("SELECT COUNT(*) FROM missions WHERE state = 'running' AND updated_at < ?",
                                    (cutoff,)).fetchone()[0]
    except Exception as exc:
        return Check("Unfinished work", "warn", f"could not be checked ({exc.__class__.__name__})")
    if runs or missions:
        return Check("Unfinished work", "warn",
                     f"{runs} request(s) and {missions} mission(s) left running for over {STUCK_MINUTES} minutes",
                     "say 'repair Jarvis' to close them (missions can then be continued)")
    return Check("Unfinished work", "ok", "nothing stuck")


def check_errors(logs_dir: Path | None) -> Check:
    if logs_dir is None:
        return Check("Recent errors", "ok", "no log folder")
    log = Path(logs_dir) / "jarvis.log"
    if not log.is_file():
        return Check("Recent errors", "ok", "no log yet")
    since = _cutoff(24 * 60)
    count, last = 0, ""
    try:
        with log.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"level": "ERROR"' in line and line[8:27] >= since[:19]:
                    count += 1
                    last = line
    except OSError:
        return Check("Recent errors", "warn", "the log could not be read")
    if count == 0:
        return Check("Recent errors", "ok", "none in the last 24 hours")
    where = ""
    if '"logger": "' in last:
        where = last.split('"logger": "', 1)[1].split('"', 1)[0]
    status = "warn" if count >= ERROR_WARN else "ok"
    return Check("Recent errors", status, f"{count} in the last 24 hours" + (f", latest in {where}" if where else ""),
                 "Menu -> Logs shows them" if status == "warn" else "")


def check_crashes(data_dir: Path) -> Check:
    folder = Path(data_dir) / "crash-reports"
    if not folder.is_dir():
        return Check("Crashes", "ok", "none recorded")
    week = time.time() - 7 * 86400
    recent = [p for p in folder.iterdir() if p.is_file() and p.stat().st_mtime >= week]
    if recent:
        return Check("Crashes", "warn", f"{len(recent)} crash report(s) this week",
                     f"they are in {folder}; 'export diagnostics' bundles them")
    return Check("Crashes", "ok", "none this week")


def check_emergency() -> Check:
    from app.core import emergency

    active = emergency.STATE.active()
    if active:
        return Check("Emergency controls", "warn", "on: " + ", ".join(active),
                     "Menu -> Emergency controls to turn them off when you're ready")
    return Check("Emergency controls", "ok", "off")


def stale_caches(source_root: Path) -> list[Path]:
    """Python cache files for THIS interpreter whose recorded source time/size no longer match the source."""
    if getattr(sys, "frozen", False):
        return []  # the installed build has no cache files next to sources
    import struct

    tag = sys.implementation.cache_tag
    stale: list[Path] = []
    for pyc in Path(source_root).rglob(f"__pycache__/*.{tag}.pyc"):
        source = pyc.parent.parent / (pyc.name.split(".")[0] + ".py")
        if not source.is_file():
            continue
        try:
            header = pyc.read_bytes()[:16]
            flags = struct.unpack("<I", header[4:8])[0]
            if flags & 1:
                continue  # hash-based cache: Python checks it itself
            mtime, size = struct.unpack("<II", header[8:16])
            st = source.stat()
            if mtime != (int(st.st_mtime) & 0xFFFFFFFF) or size != (st.st_size & 0xFFFFFFFF):
                stale.append(pyc)
        except (OSError, struct.error):
            continue
    return stale


def check_caches(source_root: Path | None) -> Check:
    if source_root is None:
        return Check("Code caches", "ok", "not applicable")
    stale = stale_caches(source_root)
    if stale:
        return Check("Code caches", "warn", f"{len(stale)} out-of-date Python cache file(s)",
                     "say 'repair Jarvis' to clear them (Python rebuilds them)")
    return Check("Code caches", "ok", "up to date")


def check_ollama(host: str) -> Check:
    try:
        import httpx

        response = httpx.get(host.rstrip("/") + "/api/tags", timeout=2.0)
        models = [m.get("name", "") for m in response.json().get("models", [])]
    except Exception:
        return Check("Local AI (Ollama)", "fail", f"not answering at {host}", "start Ollama, or restart Jarvis")
    if not models:
        return Check("Local AI (Ollama)", "warn", "running but no models installed", "ollama pull <model>")
    return Check("Local AI (Ollama)", "ok", f"running, {len(models)} model(s)")


def diagnose(ctx, *, current_run: str | None = None, source_root: Path | None = None,
             include_network: bool = True) -> list[Check]:
    checks: list[Check] = []
    if ctx.database is not None:
        checks += [check_database(ctx.database), check_stuck(ctx.database, current_run)]
    if ctx.paths is not None:
        checks += [check_disk(ctx.paths.data_dir), check_errors(getattr(ctx.paths, "logs_dir", None)),
                   check_crashes(ctx.paths.data_dir)]
    checks.append(check_emergency())
    checks.append(check_caches(source_root))
    if include_network and getattr(ctx.settings, "ai_provider", "") == "ollama":
        checks.append(check_ollama(ctx.settings.ollama_host))
    return checks


def summary(checks: list[Check]) -> str:
    bad = [c for c in checks if c.status != "ok"]
    head = "All checks passed." if not bad else f"{len(bad)} of {len(checks)} checks need attention."
    return head + " " + " ".join(c.line() for c in checks)


def to_dicts(checks: list[Check]) -> list[dict[str, Any]]:
    return [asdict(c) for c in checks]


def repair(ctx, *, current_run: str | None = None, source_root: Path | None = None) -> list[str]:
    """Fix Jarvis's own bookkeeping. Returns what was done (empty = nothing needed). Never touches your files."""
    done: list[str] = []
    db = ctx.database
    if db is not None:
        cutoff = _cutoff(STUCK_MINUTES)
        with db.transaction() as conn:
            runs = conn.execute("UPDATE agent_runs SET state = 'interrupted' WHERE state = 'running' AND "
                                "started_at < ? AND id != ?", (cutoff, current_run or "")).rowcount
            missions = conn.execute("UPDATE missions SET state = 'paused', note = 'interrupted (repaired)' "
                                    "WHERE state = 'running' AND updated_at < ?", (cutoff,)).rowcount
        if runs:
            done.append(f"closed {runs} request(s) left running")
        if missions:
            done.append(f"paused {missions} stuck mission(s) - say 'continue my mission' to resume")
        try:
            from app.remote.mobile import SessionStore

            expired = SessionStore(db).expire_idle()
            if expired:
                done.append(f"ended {expired} idle phone session(s)")
        except Exception:
            pass
    if source_root is not None:
        removed = 0
        for pyc in stale_caches(source_root):
            try:
                pyc.unlink()
                removed += 1
            except OSError:
                continue
        if removed:
            done.append(f"cleared {removed} out-of-date Python cache file(s)")
    return done
