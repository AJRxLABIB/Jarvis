"""3.25: explainability ('why did you do that?') from the audit trail, and diagnostics/recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.agent.tool_manager import ToolExecutor, ToolRegistry
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import diagnostics, explain, runs
from app.core.runs import Run, RunStore
from app.memory.audit import AuditLog
from app.memory.database import Database
from app.security.permissions import PermissionEngine, RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolResult
from tests.test_tools import ScriptedConfirmer


class NameArgs(ToolArgs):
    name: str = "x"


def _db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "j.db")
    d.initialize()
    return d


def _executor(db, confirm=True):
    def look(a, c):
        return ToolResult.success("looked")

    def change(a, c):
        return ToolResult.success("changed")

    tools = [Tool("look", "r", look, NameArgs, RiskLevel.SAFE, "info"),
             Tool("change", "m", change, NameArgs, RiskLevel.MODERATE, "files")]
    ctx = ToolContext(Settings(), database=db)
    return ToolExecutor(ToolRegistry(tools), PermissionEngine(PermissionsConfig(confirm_moderate=True)), ctx,
                        confirmer=ScriptedConfirmer(confirm), audit=AuditLog(db))


def _run(ex, source, text, *calls):
    store = RunStore(ex.context.database)
    run = Run(source=source, text=text)
    store.start(run)
    with runs.running(run, store):
        for name, args in calls:
            asyncio.run(ex.execute(ToolCall(name, args), user_command=False))
    store.finish(run, "done", 5)
    return run


# ------------------------------------------------------------------ explainability


def test_explain_reports_recorded_steps(tmp_path: Path) -> None:
    db = _db(tmp_path)
    ex = _executor(db, confirm=True)
    run = _run(ex, "voice", "tidy my desktop", ("look", {"name": "desktop"}), ("change", {"name": "a"}))
    store = RunStore(db)
    text, steps = explain.explain_run(dict([(k, run.__dict__.get(k)) for k in ("id", "source", "text")]) |
                                      {"tools": "look,change"}, store.trace(run.id))
    assert "by voice" in text and "tidy my desktop" in text
    tools = [s.tool for s in steps]
    assert tools == ["look", "change"]
    change = next(s for s in steps if s.tool == "change")
    assert change.asked and change.answer == "approved"  # moderate -> asked, approved
    look = next(s for s in steps if s.tool == "look")
    assert look.level == 0 and look.ok is True


def test_explain_shows_a_refusal(tmp_path: Path) -> None:
    db = _db(tmp_path)
    ex = _executor(db, confirm=False)  # say no
    run = _run(ex, "text", "delete stuff", ("change", {"name": "a"}))
    store = RunStore(db)
    _, steps = explain.explain_run({"id": run.id, "source": "text", "text": "delete stuff"}, store.trace(run.id))
    step = steps[0]
    assert step.asked and step.answer == "denied" and step.ok is False and step.error == "declined"
    assert "you said no" in explain.describe_step(step)


def test_pick_run_prefers_matching_and_skips_current(tmp_path: Path) -> None:
    db = _db(tmp_path)
    ex = _executor(db)
    r1 = _run(ex, "voice", "check the weather", ("look", {"name": "w"}))
    r2 = _run(ex, "voice", "open my files", ("change", {"name": "f"}))
    recent = RunStore(db).recent(10)
    assert explain.pick_run(recent, exclude=None).get("id") == r2.id  # newest with tools
    assert explain.pick_run(recent, about="weather").get("id") == r1.id
    assert explain.pick_run(recent, exclude=r2.id).get("id") == r1.id


def test_explain_when_nothing_was_done() -> None:
    text, steps = explain.explain_run({"source": "voice", "text": "hello"}, [])
    assert steps == [] and "only answered" in text


# ------------------------------------------------------------------ diagnostics


def test_diagnose_reports_stuck_work(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)

    old = "2020-01-01T00:00:00+00:00"
    with db.transaction() as conn:
        conn.execute("INSERT INTO agent_runs (id, source, text, state, started_at) VALUES ('r1','voice','x','running',?)",
                     (old,))
    stuck = diagnostics.check_stuck(db, None)
    assert stuck.status == "warn" and "request" in stuck.detail
    healthy = diagnostics.check_database(db)
    assert healthy.status == "ok"


def test_repair_closes_stuck_runs_and_missions(tmp_path: Path) -> None:
    db = _db(tmp_path)
    old = "2020-01-01T00:00:00+00:00"
    with db.transaction() as conn:
        conn.execute("INSERT INTO agent_runs (id, source, text, state, started_at) VALUES ('r1','voice','x','running',?)",
                     (old,))
        conn.execute("INSERT INTO missions (id, title, state, created_at, updated_at) VALUES "
                     "('m1','job','running',?,?)", (old, old))
    ctx = ToolContext(Settings(), database=db)
    done = diagnostics.repair(ctx)
    assert any("request" in d for d in done) and any("mission" in d for d in done)
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM agent_runs WHERE id='r1'").fetchone()[0] == "interrupted"
        assert conn.execute("SELECT state FROM missions WHERE id='m1'").fetchone()[0] == "paused"
    assert diagnostics.repair(ctx) == []  # nothing left to fix


def test_disk_check() -> None:

    c = diagnostics.check_disk(Path("."))
    assert c.status in ("ok", "warn", "fail") and "free" in c.detail.lower() or c.status == "warn"


def test_stale_cache_detection(tmp_path: Path) -> None:
    import struct
    import sys

    pkg = tmp_path / "pkg"
    (pkg / "__pycache__").mkdir(parents=True)
    src = pkg / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    tag = sys.implementation.cache_tag
    pyc = pkg / "__pycache__" / f"mod.{tag}.pyc"
    # write a timestamp-based header that does NOT match the source (stale)
    pyc.write_bytes(b"\x00" * 4 + struct.pack("<I", 0) + struct.pack("<II", 12345, 999) + b"\x00" * 8)
    stale = diagnostics.stale_caches(tmp_path)
    assert pyc in stale
    # matching header -> not stale
    st = src.stat()
    pyc.write_bytes(b"\x00" * 4 + struct.pack("<I", 0)
                    + struct.pack("<II", int(st.st_mtime) & 0xFFFFFFFF, st.st_size & 0xFFFFFFFF) + b"\x00" * 8)
    assert diagnostics.stale_caches(tmp_path) == []


def test_commands():
    from app.agent.shortcuts import match_shortcut as m

    assert m("why did you do that", lambda n: None).name == "explain_action"
    assert m("diagnose yourself", lambda n: None).name == "diagnose_jarvis"
    assert m("repair yourself", lambda n: None).name == "repair_jarvis"
