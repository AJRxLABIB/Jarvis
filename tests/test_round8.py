from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config.settings import Settings
from app.core import auditchain, procstats, timeline
from app.core.research_store import ResearchStore
from app.core.upkeep import Upkeep
from app.memory.audit import AuditLog
from app.memory.database import Database
from app.privacy import TABLES
from app.tools.base import ToolContext
from app.tools.diagnostics_tools import ProgramArgs, TimelineArgs, activity_timeline, program_history

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "r8.db")
    db.initialize()
    return db


# ----------------------------------------------------------------------- audit chain


def filled(db: Database, count: int = 6) -> AuditLog:
    log = AuditLog(db)
    for i in range(count):
        log.record("tool_result", tool=f"t{i}", level=0, decision="allow", details={"n": i})
    return log


def test_chain_verifies_and_detects_edits(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    filled(db)
    assert auditchain.verify(db).ok and auditchain.verify(db).checked == 6
    with db.transaction() as conn:
        conn.execute("UPDATE audit_log SET decision = 'deny' WHERE id = 3")
    report = auditchain.verify(db)
    assert not report.ok and any("entry 3 was changed" in p for p in report.problems)


def test_chain_detects_removed_middle_and_tail(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    filled(db)
    with db.transaction() as conn:
        conn.execute("DELETE FROM audit_log WHERE id = 3")
    assert not auditchain.verify(db).ok
    (tmp_path / "x").mkdir()
    db2 = make_db(tmp_path / "x")
    filled(db2)
    with db2.transaction() as conn:
        conn.execute("DELETE FROM audit_log WHERE id = 6")
    assert "newest" in auditchain.verify(db2).describe()


def test_confirmed_deletes_keep_chain_valid(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    filled(db)
    with db.transaction() as conn:   # the oldest entries removed, as delete_older_than does
        conn.execute("DELETE FROM audit_log WHERE id <= 2")
    assert auditchain.verify(db).ok
    assert timeline.delete_older_than(db, 0, datetime.now(timezone.utc) + timedelta(days=1)) == 4
    assert auditchain.verify(db).ok
    AuditLog(db).record("tool_result", tool="after", details={})
    assert auditchain.verify(db).ok
    # privacy "delete all" resets the head as well
    with db.transaction() as conn:
        for sql in TABLES["activity"].sql_delete:
            conn.execute(sql)
    assert auditchain.verify(db).ok


def test_verify_through_the_tool(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    filled(db, 3)
    ctx = ToolContext(Settings(), database=db)
    assert activity_timeline(TimelineArgs(action="verify"), ctx).data["intact"] is True


# ------------------------------------------------------------------- program history


def seed_processes(db: Database, count: int = 40, mem: float = 300.0) -> None:
    store = procstats.ProcessStore(db)
    for i in range(count):
        store.record([{"name": "chrome.exe", "cpu": 10.0, "mem_mb": mem + (i % 3)}], NOW - timedelta(minutes=count - i))


def test_program_baseline_and_unusual(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    store = procstats.ProcessStore(db, timedelta(days=3650))
    for i in range(40):
        store.record([{"name": "chrome.exe", "cpu": 10.0, "mem_mb": 300.0}], NOW - timedelta(minutes=i))
    assert store.baselines()["chrome.exe"]["samples"] == 40
    odd = store.unusual([{"name": "chrome.exe", "cpu": 5.0, "mem_mb": 900.0}, {"name": "new.exe", "cpu": 1.0, "mem_mb": 5000.0}])
    assert [o["name"] for o in odd] == ["chrome.exe"] and odd[0]["basis"] == "inferred"
    assert store.unusual([{"name": "chrome.exe", "cpu": 5.0, "mem_mb": 320.0}]) == []   # near its normal


def test_crash_correlation_is_labelled_a_correlation(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    store = procstats.ProcessStore(db, timedelta(days=3650))
    for hour in (1, 3, 5):
        at = NOW - timedelta(hours=hour)
        with db.transaction() as conn:
            conn.execute("INSERT INTO agent_runs (id, source, state, started_at) VALUES (?, 'typed', 'failed', ?)",
                         (f"r{hour}", at.isoformat()))
        store.record([{"name": "leaky.exe", "cpu": 5.0, "mem_mb": 4000.0}], at - timedelta(minutes=3))
    found = store.around_failures(now=NOW)
    assert len(found) == 3 and found[0]["times_before_failures"] == 3 and found[0]["confidence"] == "medium"
    text = procstats.describe([], found, True)
    assert "leaky.exe" in text and "not proof" in text


def test_program_history_tool_and_default_off(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    ctx = ToolContext(Settings(), database=db)
    assert Settings().process_history is False
    assert "off by default" in program_history(ProgramArgs(action="report"), ctx).message
    seed_processes(db)
    assert program_history(ProgramArgs(action="delete"), ctx).data["removed"] == 40


def test_upkeep_records_programs_only_when_enabled(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    reading = {"cpu": 1.0, "mem_percent": 2.0, "disk_percent": 3.0, "battery": None, "plugged": None}
    procs = [{"name": "a.exe", "cpu": 1.0, "mem_mb": 10.0}]
    for enabled in (False, True):
        settings = Settings(process_history=enabled, auto_backup=False)
        up = Upkeep(db, tmp_path / "r8.db", tmp_path / "b", settings=settings, sampler=lambda: reading,
                    clock=lambda: NOW + timedelta(minutes=int(enabled) * 5), process_sampler=lambda: procs)
        up.store.record = lambda r, now=None: True  # type: ignore[method-assign]
        up.run_once()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM process_samples").fetchone()[0] == 1


# -------------------------------------------------------------- research monitoring


def test_watch_marks_changed_sources_and_respects_interval(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    store = ResearchStore(db)
    store.create("laptops", "best laptop?")
    store.add_source("laptops", "https://example.com/a", "price 999")
    sid = store.sources(store.project("laptops")["id"])[0]["id"]
    store.add_claim("laptops", "price", "costs 999", sid)
    assert store.due() == []                       # not watched unless you opt in
    store.watch("laptops", 7)
    assert store.due() == ["laptops"]
    up = Upkeep(db, tmp_path / "r8.db", tmp_path / "b", settings=Settings())

    async def changed(url: str) -> str:
        return "price 899"

    assert asyncio.run(up.watch_research(changed)) == ["laptops"]
    assert store.claims(store.project("laptops")["id"])[0]["needs_recheck"] == 1
    assert store.due() == []                       # checked just now: not due for 7 days
    store.watch("laptops", 0)
    assert store.due(NOW + timedelta(days=30)) == []


def test_watch_failure_to_fetch_changes_nothing(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    store = ResearchStore(db)
    store.create("p", "q")
    store.add_source("p", "https://example.com/a", "text")
    store.watch("p", 1)
    up = Upkeep(db, tmp_path / "r8.db", tmp_path / "b", settings=Settings())

    async def broken(url: str) -> str:
        raise OSError("offline")

    assert asyncio.run(up.watch_research(broken)) == []
