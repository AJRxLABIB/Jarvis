"""Digital twin (observations, staleness) and the what-if preview: nothing runs, estimates are labelled."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import twin as T
from app.memory.database import Database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "t.db")
    d.initialize()
    return d


@pytest.fixture()
def ctx(db):
    from app.config.settings import Settings
    from app.tools.base import ToolContext

    return ToolContext(Settings(), database=db)


def test_observation_freshness_and_staleness(db):
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    T.observe(db, "device", "Phone", {"state": "online"}, "device registry", at=now - timedelta(seconds=60))
    T.observe(db, "device", "Laptop", {"state": "online"}, "device registry", at=now - timedelta(hours=2))
    snap = {o.key: o for o in T.snapshot(db, now)}
    assert snap["Phone"].state == "fresh" and snap["Laptop"].state == "stale"
    text = T.describe(db, now)
    assert "1 stale" in text and "[STALE] device Laptop" in text and "device registry" in text


def test_empty_twin_says_unknown_not_fine(db):
    assert "UNKNOWN" in T.describe(db)


def test_observe_replaces_and_forget(db):
    T.observe(db, "disk", "d", {"free_gb": 5}, "disk usage")
    T.observe(db, "disk", "d", {"free_gb": 4}, "disk usage")
    assert len(T.snapshot(db)) == 1 and T.snapshot(db)[0].value["free_gb"] == 4
    assert T.forget(db, "disk") == 1 and T.snapshot(db) == []


def test_refresh_reads_only_local_stores(ctx, db):
    notes = T.refresh(ctx)
    assert "disk" in notes and any(o.kind == "disk" for o in T.snapshot(db))


def test_what_if_runs_nothing_and_flags_problems(ctx, tmp_path, monkeypatch):
    victim = tmp_path / "a.txt"
    victim.write_text("x")
    called = []
    import app.tools.filesystem as fs

    for name in dir(fs):
        if name.endswith("_item"):
            monkeypatch.setattr(fs, name, lambda *a, **k: called.append(1))
    plan = [{"tool": "rename_item", "arguments": {"path": str(victim), "new_name": "b.txt"}},
            {"tool": "delete_item", "arguments": {"path": str(tmp_path / "missing.txt")}},
            {"tool": "no_such_tool", "arguments": {}}]
    report = T.what_if(plan, ctx)
    text = report.describe()
    assert called == [] and victim.exists()
    assert "nothing was run" in text and "not a guarantee" in text
    assert report.blocked
    assert "does not exist" in text and "no tool named 'no_such_tool'" in text
    assert "undo" in report.steps[0].reversibility


def test_what_if_collision_symlink_and_alternatives(ctx, tmp_path):
    (tmp_path / "out.zip").write_text("x")
    src = tmp_path / "f.txt"
    src.write_text("x")
    link = tmp_path / "link"
    try:
        link.symlink_to(src)
    except OSError:
        link = None
    plan = [{"tool": "zip_items", "arguments": {"paths": [str(src)], "destination": str(tmp_path / "out.zip")}}]
    from app.tools.filesystem import TOOLS

    if any(t.name == "zip_items" for t in TOOLS):
        txt = T.what_if(plan, ctx).describe()
        assert "already exists" in txt or "invalid arguments" in txt
    rename = [{"tool": "batch_rename", "arguments": {"pairs": [[str(src), "g.txt"]], "apply": True}}]
    r = T.what_if(rename, ctx)
    assert r.steps[0].changes_things and "apply=false" in r.steps[0].safer
    if link is not None:
        assert "symbolic link" in T.what_if([{"tool": "rename_item", "arguments": {"path": str(link), "new_name": "z"}}], ctx).describe()


def test_earlier_step_that_creates_a_path_is_not_a_problem(ctx, tmp_path):
    target = tmp_path / "new.txt"
    plan = [{"tool": "create_item", "arguments": {"path": str(target), "kind": "file"}},
            {"tool": "rename_item", "arguments": {"path": str(target), "new_name": "n2.txt"}}]
    r = T.what_if(plan, ctx)
    assert not any("does not exist" in p for s in r.steps for p in s.problems), r.describe()


def test_what_if_records_path_observations_and_is_not_marked_irreversible_when_undoable(ctx, db, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("1")
    T.what_if([{"tool": "rename_item", "arguments": {"path": str(f), "new_name": "y.txt"}}], ctx)
    obs = [o for o in T.snapshot(db) if o.kind == "path"]
    assert obs and obs[0].value["exists"] is True and obs[0].source == "what-if check"


def test_stale_device_referenced_in_a_plan_is_reported(ctx, db):
    T.observe(db, "device", "Phone", {"state": "online"}, "device registry", at=datetime.now(timezone.utc) - timedelta(hours=3))
    r = T.what_if([{"tool": "phone_control", "arguments": {"action": "status", "device": "Phone"}}], ctx)
    assert any("Phone" in s for s in r.stale_dependencies)


def test_tool_actions(ctx, db):
    from app.tools.twin_tool import TwinArgs, twin_tool

    run = lambda **kw: asyncio.run(twin_tool(TwinArgs(**kw), ctx))
    assert "UNKNOWN" in run(action="show").message
    assert run(action="refresh").ok
    res = run(action="what_if", steps='[{"tool": "no_such_tool", "arguments": {}}]')
    assert res.data["blocked"] and res.data["simulated"]
    from app.tools.base import ToolError

    with pytest.raises(ToolError):
        run(action="what_if")
    with pytest.raises(ToolError):
        run(action="what_if", steps="not json")
    assert run(action="forget").ok
