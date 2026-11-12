import pytest

from app.core import mission_report as MR, project_move as PM
from app.memory.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.initialize()
    return d


class Tool:
    def __init__(self, level, verify=None):
        self.level, self.verify = level, verify


class Reg:
    def __init__(self, tools):
        self.tools = tools

    def get(self, name):
        return self.tools.get(name)


def _mission(db, steps):
    from app.core import missions as M

    store = M.MissionStore(db)
    m = store.create("Deploy site", [M.Step(i + 1, t, tool, {}) for i, (t, tool, _, _) in enumerate(steps)])
    for step, (_, _, state, result) in zip(m.steps, steps):
        step.state, step.result, step.attempts = state, result, 1 if state in ("done", "failed") else 0
        store.save_step(m, step)
    store.set_state(m, "paused", "Paused at step 2")
    return store.get(m.id)


def test_report_separates_done_failed_not_run_and_remaining(db):
    m = _mission(db, [("check disk", "pc_health", "done", "ok"), ("upload files", "upload", "failed", "connection timed out"),
                      ("notify", "send_note", "skipped", "skipped: needs step 2, which didn't run"), ("tidy", "clean", "pending", "")])
    r = MR.report(m, Reg({"pc_health": Tool(0), "upload": Tool(1), "send_note": Tool(1), "clean": Tool(1)}))
    assert r["done"] == ["1. check disk"] and r["remaining"] == ["4. tidy"] and len(r["not_run"]) == 1
    f = r["failed"][0]
    assert f["tool"] == "upload" and "temporary" in f["probable_cause"] and f["retry_safe"] is False and "check first" in f["retry_note"]
    text = MR.describe(r)
    assert "FAILED: 2. upload files" in text and "not proven" in text and "Not run (1)" in text and "Still to do (1)" in text


def test_retry_is_safe_only_for_read_only_steps(db):
    m = _mission(db, [("read status", "pc_health", "failed", "timed out"), ("write file", "write_file", "failed", "access denied"), ("gone", "old_tool", "failed", "x")])
    r = MR.report(m, Reg({"pc_health": Tool(0), "write_file": Tool(1)}))
    safe = {f["tool"]: f["retry_safe"] for f in r["failed"]}
    assert safe == {"pc_health": True, "write_file": False, "old_tool": False}
    assert "no longer exists" in [f for f in r["failed"] if f["tool"] == "old_tool"][0]["retry_note"]
    verified_read = MR.report(_mission(db, [("check", "verify_tool", "failed", "timed out")]), Reg({"verify_tool": Tool(0, verify=lambda *a: (True, ""))}))
    assert verified_read["failed"][0]["retry_safe"] is False           # a tool that verifies a change is not treated as a plain read


def test_analytics_needs_history_and_counts_real_rows(db):
    assert MR.analytics(db)["enough"] is False and "Not enough finished missions" in MR.describe_analytics(MR.analytics(db))
    with db.transaction() as conn:
        for i in range(6):
            state = "completed" if i < 4 else "failed"
            conn.execute("INSERT INTO missions (id, title, state, created_at, updated_at) VALUES (?,?,?,?,?)",
                         (f"m{i}", f"M{i}", state, "2026-11-10T10:00:00+00:00", f"2026-11-10T10:{10 + i * 10}:00+00:00"))
            conn.execute("INSERT INTO mission_steps (mission_id, n, title, tool, state) VALUES (?,?,?,?,?)", (f"m{i}", 1, "s", "upload", "done" if i < 4 else "failed"))
        conn.execute("INSERT INTO missions (id, title, state, created_at, updated_at) VALUES ('open','O','running','2026-11-10T10:00:00+00:00','2026-11-10T10:05:00+00:00')")
    a = MR.analytics(db)
    assert a["enough"] and a["finished"] == 6 and a["completed"] == 4 and a["completion_rate"] == 0.67 and a["open"] == 1
    assert a["median_minutes"] == 25.0 and a["failing_tools"] == [{"tool": "upload", "failed": 2, "of": 6}]
    assert "67%" in MR.describe_analytics(a) and "upload 2 of 6" in MR.describe_analytics(a)


def test_project_move_finds_venv_paths_pointer_and_running(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".venv").mkdir(parents=True)
    (proj / ".venv" / "pyvenv.cfg").write_text("home = C:/Python")
    (proj / ".venv" / "big.txt").write_text(str(proj))                 # inside the venv: not listed again
    (proj / "src").mkdir()
    (proj / "src" / "settings.json").write_text('{"x": 1,\n "root": "' + str(proj).replace("\\", "/") + '/data"}')
    (proj / "src" / "clean.py").write_text("print('no paths here')")
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "pkg.json").write_text(str(proj))         # dependency folder: skipped
    (proj / "sub").mkdir()
    (proj / "sub" / ".git").write_text("gitdir: ../.git/modules/sub")
    (proj / "image.bin").write_bytes(b"\x00" + str(proj).encode())
    rep = PM.check(str(proj), lambda: [("node.exe", str(proj / "src")), ("other.exe", str(tmp_path))])
    kinds = sorted(f.kind for f in rep.findings)
    assert kinds == ["git_pointer", "path_in_file", "running", "venv"]
    hit = [f for f in rep.findings if f.kind == "path_in_file"][0]
    assert hit.where.endswith("settings.json:2")
    text = PM.describe(rep, "D:/proj")
    assert "nothing was moved or run" in text and "recreate any virtual environment" in text and "Not checked" in text and "registry" in text
    assert str(proj) not in text.split("Static check of")[1].split("\n", 1)[1]        # contents are not echoed, only where


def test_project_move_clean_folder_and_errors(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    rep = PM.check(str(tmp_path))
    assert rep.findings == [] and "running programs" in rep.not_checked[0]
    assert "should not break" in PM.describe(rep)
    with pytest.raises(FileNotFoundError):
        PM.check(str(tmp_path / "missing"))

    def broken():
        raise OSError("denied")

    assert any("could not be read" in n for n in PM.check(str(tmp_path), broken).not_checked)


def test_memory_eval_passes_on_the_real_memory_code_and_reports_failures():
    from app import memory_eval as E

    results = E.run()
    assert len(results) == len(E.CASES) and all(r.passed for r in results), [r for r in results if not r.passed]
    text = E.report(results + [E.CaseResult("made-up failing case", False, "the old value is still recalled")])
    assert "FAIL  made-up failing case" in text and f"{len(E.CASES)} of {len(E.CASES) + 1}" in text and "not touched" in text


def test_tools_registered():
    from app.tools.catalog import build_default_registry

    reg = build_default_registry()
    assert reg.get("mission_report") is not None and reg.get("project_move_check") is not None
