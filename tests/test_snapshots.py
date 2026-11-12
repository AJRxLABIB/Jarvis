import asyncio

from app.core import snapshots as S


class Mem:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


def live(files=None, windows=(), mission=None):
    files = files or {}
    return S.Live(windows=lambda: list(windows), exists=lambda p: p in files or p.endswith("exe") and p in files, mtime=lambda p: files.get(p),
                  mission=lambda mid: mission)


def snap(**kw):
    base = dict(name="thesis", folder="/p", files=["/p/a.md", "/p/b.md"], windows=[{"process": "code.exe", "path": "/bin/code.exe", "title": "SECRET"}],
                mission={"id": "m1", "title": "build", "state": "paused", "done": 1, "total": 3}, questions=["which font?"],
                mtime=lambda p: 100.0)
    base.update(kw)
    return S.capture(**base)


def test_capture_drops_titles_and_versions_increase():
    s = snap()
    assert "title" not in s["windows"][0] and s["version"] == 1
    st = Mem(); S.save(st, s)
    assert S.next_version(st, "thesis") == 1 and snap(previous_version=1)["version"] == 2
    for i in range(25): S.save(st, snap(name=f"n{i}"))
    assert len(S._load(st)) == S.MAX_SNAPSHOTS


def test_compare_reports_every_difference():
    s = snap()
    f = S.compare(s, live({"/p": 0, "/p/a.md": 100.0, "/bin/code.exe": 0}, windows=[], mission={"state": "running", "done": 1, "total": 3, "next": 2}))
    kinds = {x.what: x.kind for x in f}
    assert kinds["file /p/a.md"] == "ok" and kinds["file /p/b.md"] == "missing"
    assert kinds["code.exe is not open"] == "gone"
    assert any(x.kind == "uncertain" and "may or may not" in x.detail for x in f)
    changed = S.compare(s, live({"/p": 0, "/p/a.md": 500.0, "/p/b.md": 100.0}, windows=[{"process": "code.exe"}],
                                mission={"state": "paused", "done": 2, "total": 3}))
    assert {x.kind for x in changed if x.what.startswith("file /p/a")} == {"changed"}
    assert any(x.kind == "changed" and "mission" in x.what for x in changed)


def test_resume_opens_only_what_exists_and_never_replays():
    s = snap()
    f = S.compare(s, live({"/p": 0, "/p/a.md": 100.0}, mission={"state": "running", "done": 1, "total": 3, "next": 2}))
    plan = S.resume_plan(s, f)
    assert [x["arguments"]["path"] for x in plan["steps"]] == ["/p", "/p/a.md"]
    assert plan["needs_your_decision"] and any("b.md" in x for x in plan["skipped"])
    assert all(x["tool"] == "open_path" for x in plan["steps"])               # nothing closed, saved or overwritten
    assert "open questions" in S.describe(s, f)


def test_recovery_report_for_an_interrupted_mission(tmp_path):
    from app.core.missions import MissionStore, validate_plan
    from app.core.reliability_lab import _executor

    ex, ran, _, db = _executor(tmp_path, True)
    store = MissionStore(db)
    m = store.create("lab", validate_plan([{"title": "one", "tool": "canary_safe", "arguments": {}},
                                           {"title": "two", "tool": "canary_moderate", "arguments": {}}], ex.registry, ex.context))
    m.steps[0].state = "done"; store.save_step(m, m.steps[0])
    m.steps[1].state = "running"; store.save_step(m, m.steps[1]); store.set_state(m, "running")
    store.mark_interrupted()
    rep = S.recovery_report(db)
    assert rep and rep[0]["uncertain"] and "step 1" in rep[0]["last_confirmed"]
    assert "check what step 2" in rep[0]["options"][0]
    assert "UNCERTAIN" in S.recovery_text(rep) and S.recovery_text([]) == "Nothing was left unfinished."


def test_tool_save_show_and_recovery(tmp_path):
    from app.core.reliability_lab import _executor
    from app.tools import snapshot_tool as T

    ex, _, _, db = _executor(tmp_path, True)
    f = tmp_path / "a.txt"; f.write_text("x")
    ctx = ex.context
    ctx.executor = ex
    run = lambda **kw: asyncio.run(T.work_snapshot(T.SnapArgs(**kw), ctx))      # noqa: E731
    assert "Saved 'demo' v1" in run(action="save", name="demo", files=[str(f)], notes="n").message
    assert "[OK]" in run(action="show", name="demo").message
    f.unlink()
    assert "[MISSING]" in run(action="show", name="demo").message
    assert run(action="recovery").message == "Nothing was left unfinished."
    assert "demo v1" in run(action="list").message
    assert run(action="delete", name="demo").ok
