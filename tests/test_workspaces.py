"""Workspace objects: save/preview/restore, missing-item reporting, per-signal switches, restore through the executor."""

import asyncio
from pathlib import Path

import pytest

from app.core import workspaces as W
from app.memory.database import Database
from app.tools.base import ToolContext, ToolError


@pytest.fixture()
def ctx(tmp_path: Path):
    from app.config.settings import Settings
    from app.utils.paths import AppPaths

    db = Database(tmp_path / "w.db")
    db.initialize()
    c = ToolContext(Settings(), database=db)
    c.paths = AppPaths.build(tmp_path / "app")
    c.paths.data_dir.mkdir(parents=True)
    return c


def project(tmp_path):
    folder = tmp_path / "thesis"
    folder.mkdir(exist_ok=True)
    (folder / "main.tex").write_text("x")
    return folder


def layouts(ctx, tmp_path, exe=None):
    from app.tools.layouts import _store

    _store(ctx, {"writing": {"saved": "now", "windows": [{"process": "word.exe", "path": str(exe or tmp_path / "nowhere" / "word.exe"),
                                                           "title": "t", "rect": [0, 0, 5, 5], "maximized": False}]}})


def test_save_show_and_steps(ctx, tmp_path):
    folder = project(tmp_path)
    layouts(ctx, tmp_path)
    W.save(ctx, "Thesis", folder=str(folder), files=[str(folder / "main.tex")], layout="writing", notes="chapter 3")
    pv = W.preview(ctx, "thesis")
    text = pv.describe()
    assert "nothing has been opened yet" in text and "chapter 3" in text
    assert [s["tool"] for s in pv.steps] == ["open_path", "open_path", "arrange_windows"]
    assert "can no longer be started: word.exe" in text


def test_missing_things_are_reported_and_their_steps_left_out(ctx, tmp_path):
    folder = project(tmp_path)
    W.save(ctx, "thesis", folder=str(folder), files=[str(folder / "main.tex")])
    (folder / "main.tex").unlink()
    pv = W.preview(ctx, "thesis")
    assert [i.signal for i in pv.missing] == ["files"] and [s["arguments"]["path"] for s in pv.steps] == [str(folder)]
    folder.rename(tmp_path / "gone")
    pv = W.preview(ctx, "thesis")
    assert any(i.signal == "folder" for i in pv.missing) and pv.steps == []


def test_signals_can_be_switched_off_per_workspace(ctx, tmp_path):
    folder = project(tmp_path)
    layouts(ctx, tmp_path)
    W.save(ctx, "thesis", folder=str(folder), layout="writing")
    assert W.set_ignored(ctx.database, "thesis", "layout", True) == ["layout"]
    pv = W.preview(ctx, "thesis")
    assert [s["tool"] for s in pv.steps] == ["open_path"] and any(i.status == "skipped" for i in pv.items)
    assert W.set_ignored(ctx.database, "thesis", "layout", False) == []
    with pytest.raises(ToolError):
        W.set_ignored(ctx.database, "thesis", "bogus", True)


def test_goal_and_unfinished_mission_are_joined(ctx, tmp_path):
    from app.core.goals import GoalStore
    from app.core.missions import MissionStore, Step

    folder = project(tmp_path)
    goal = GoalStore(ctx.database).add("Finish thesis")
    mission = MissionStore(ctx.database).create("Write chapter", [Step(1, "s", "web_search", {"query": "q"})], goal_id=goal.id)
    MissionStore(ctx.database).set_state(mission, "paused")
    W.save(ctx, "thesis", folder=str(folder), goal="finish")
    text = W.preview(ctx, "thesis").describe()
    assert "Finish thesis" in text and "Write chapter" in text and "resume it yourself" in text


def test_validation(ctx, tmp_path):
    with pytest.raises(ToolError):
        W.save(ctx, "x", folder=str(tmp_path / "nope"))
    with pytest.raises(ToolError):
        W.save(ctx, "x")
    with pytest.raises(ToolError):
        W.save(ctx, "x", folder=str(project(tmp_path)), layout="unknown")
    with pytest.raises(ToolError):
        W.save(ctx, "x", folder=str(project(tmp_path)), goal="nothing")
    with pytest.raises(ToolError):
        W.preview(ctx, "absent")
    with pytest.raises(ToolError):
        W.key_of("bad/name")


def test_restore_runs_steps_through_the_executor_and_reports_failures(ctx, tmp_path):
    folder = project(tmp_path)
    W.save(ctx, "thesis", folder=str(folder), files=[str(folder / "main.tex")])
    calls = []

    class Result:
        def __init__(self, ok): self.ok, self.message = ok, "done" if ok else "no"

    class Ex:
        async def execute(self, call):
            calls.append((call.name, dict(call.arguments)))
            return type("E", (), {"result": Result(len(calls) == 1)})()

    ctx.executor = Ex()
    pv, results = asyncio.run(W.restore(ctx, "thesis"))
    assert [c[0] for c in calls] == ["open_path", "open_path"] and [r[1] for r in results] == [True, False]


def test_tool_actions_and_delete(ctx, tmp_path):
    from app.tools.workspace_tool import WorkspaceArgs, workspace_tool

    run = lambda **kw: asyncio.run(workspace_tool(WorkspaceArgs(**kw), ctx))
    assert "No workspaces" in run(action="list").message
    folder = project(tmp_path)
    assert run(action="save", name="thesis", folder=str(folder)).ok
    assert "thesis" in run(action="list").message
    assert run(action="show", name="thesis").data["missing"] == 0
    assert run(action="ignore", name="thesis", signal="files").data["ignored"] == ["files"]
    assert run(action="delete", name="thesis").message == "Deleted."
    assert W.load(ctx.database) == {}
    with pytest.raises(ToolError):
        run(action="show")
