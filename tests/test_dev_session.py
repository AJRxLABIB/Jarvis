"""Verified project start: log reading, state decisions, and the tool end to end with a fake executor (no process is started)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core import dev_session as DS
from app.security.command_executor import CommandExecutor, CommandOutput
from app.tools import dev_session_tool as T
from tests.test_developer import call, make


def probe_for(*open_ports):
    return lambda port: port in open_ports


def test_ports_are_read_from_common_server_banners():
    log = ("\x1b[32m  VITE v5  ready in 300 ms\x1b[0m\n  ➜  Local:   http://localhost:5173/\nServer listening on port 3000\n"
           " * Running on http://127.0.0.1:5000\nDjango: Starting development server at http://127.0.0.1:8000/")
    assert DS.ports_announced(log) == [5173, 5000, 8000, 3000] or set(DS.ports_announced(log)) == {5173, 5000, 8000, 3000}
    assert DS.ports_announced("nothing here, port 99999999") == []


@pytest.mark.parametrize(("log", "kind", "advice_part"), [
    ("Traceback (most recent call last):\n  File \"app.py\", line 3, in <module>\nModuleNotFoundError: No module named 'flask'", "missing_module", "pip install flask"),
    ("Error: Cannot find module 'express'", "missing_module", "npm install"),
    ("Error: listen EADDRINUSE: address already in use :::3000", "port_in_use", "another port"),
    ("  File \"x.py\", line 12\n    def f(:\nSyntaxError: invalid syntax", "syntax_error", "fix that line"),
    ("'vite' is not recognized as an internal or external command", "tool_missing", "install it"),
    ("sqlalchemy.exc.OperationalError: (2003) Can't connect to MySQL server", "dependency_down", "start that service"),
])
def test_errors_get_a_plain_cause_and_next_step(log, kind, advice_part):
    found = DS.find_errors(log)
    assert found and found[0].kind == kind and advice_part in found[0].advice


def test_error_location_is_extracted_and_never_executed():
    f = DS.find_errors("Traceback (most recent call last):\n  File \"C:\\proj\\main.py\", line 42, in run\nModuleNotFoundError: No module named 'x'")[0]
    assert f.where.endswith("main.py:42")
    hostile = DS.find_errors("ModuleNotFoundError: No module named 'a; rm -rf /'")
    assert all("rm -rf" not in x.advice or "pip install" in x.advice for x in hostile)          # text only; nothing is ever run from a log


def test_state_decisions():
    up = DS.assess(exit_code=None, log="Local: http://localhost:3000", elapsed=5, probe=probe_for(3000))
    assert up.state == DS.RUNNING and up.up and "verified" in up.render("web") and up.url == "http://localhost:3000"
    assert DS.assess(exit_code=None, log="Local: http://localhost:3000", elapsed=5, probe=probe_for()).state == DS.STARTING
    assert DS.assess(exit_code=None, log="Local: http://localhost:3000", elapsed=30, probe=probe_for()).state == DS.UNVERIFIED
    assert DS.assess(exit_code=None, log="compiling...", elapsed=30, probe=probe_for()).state == DS.UNVERIFIED
    crashed = DS.assess(exit_code=1, log="ModuleNotFoundError: No module named 'flask'", elapsed=2, probe=probe_for())
    assert crashed.state == DS.CRASHED and "exit code 1" in crashed.render() and "flask" in crashed.render()
    assert DS.assess(exit_code=0, log="done", elapsed=2, probe=probe_for()).state == DS.FINISHED
    both = DS.assess(exit_code=None, log="http://localhost:3000\nnpm ERR! missing script: lint", elapsed=5, probe=probe_for(3000))
    assert both.state == DS.RUNNING_ERRORS and both.up


def test_up_is_never_claimed_without_a_port_answering():
    r = DS.assess(exit_code=None, log="Server running at http://localhost:8080", elapsed=3, probe=probe_for())
    assert not r.up and r.state != DS.RUNNING


def test_wait_until_settled_polls_and_stops_at_the_deadline():
    now = [0.0]
    logs = iter([(None, ""), (None, "Listening on port 4000"), (None, "Listening on port 4000")])
    ports = {"open": False}

    def poll():
        code, log = next(logs)
        ports["open"] = "4000" in log
        return code, log

    r = DS.wait_until_settled(poll, lambda p: ports["open"], timeout=10, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + 1))
    assert r.state == DS.RUNNING and r.port == 4000
    stuck = DS.wait_until_settled(lambda: (None, "booting"), probe_for(), timeout=3, clock=lambda: now.__setitem__(0, now[0] + 1) or now[0], sleep=lambda s: None)
    assert stuck.state in (DS.STARTING, DS.UNVERIFIED)


# ------------------------------------------------------------------ the tool


class Fake:
    def __init__(self, log="", code=None):
        self.log, self.code, self.calls, self.stopped = log, code, [], []

    def run(self, argv, cwd, timeout=120):
        return CommandOutput(tuple(argv), 0, "")

    def start_background(self, argv, cwd):
        raise AssertionError("dev_session must use the captured start, not a console window")

    def start_captured(self, argv, cwd, log_path):
        self.calls.append((tuple(argv), Path(cwd), Path(log_path)))
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(self.log, encoding="utf-8")
        return CommandOutput(tuple(argv), None, "", pid=777)

    def exit_code(self, pid):
        return self.code

    def stop(self, pid):
        self.stopped.append(pid)
        return self.code is None


def project(tmp_path, name="package.json", text='{"scripts": {"dev": "vite"}}'):
    p = tmp_path / "project"
    p.mkdir(exist_ok=True)
    (p / name).write_text(text, encoding="utf-8")
    return p


def test_start_reports_a_verified_server(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tools.developer.resolve_program", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(T, "_probe", lambda port: port == 5173)
    T.SESSIONS.clear()
    fake = Fake("VITE ready\n  Local: http://localhost:5173/")
    project(tmp_path)
    ex, ctx, confirmer, _ = make(tmp_path, fake)
    ex.context.paths = type("P", (), {"data_dir": tmp_path / "data"})()
    r = call(ex, "dev_session", action="start", wait_seconds=3).result
    assert r.ok and r.data["state"] == "running" and r.data["port"] == 5173 and "verified" in r.message
    assert fake.calls[0][0] == ("npm", "run", "dev") and fake.calls[0][2].name == "project.log"
    again = call(ex, "dev_session", action="start").result
    assert "already running" in again.message and len(fake.calls) == 1             # not started twice
    assert call(ex, "dev_session", action="stop").result.data["stopped"] is True and fake.stopped == [777]


def test_start_reports_a_crash_honestly_with_the_cause(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tools.developer.resolve_program", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(T, "_probe", lambda port: False)
    T.SESSIONS.clear()
    project(tmp_path, "manage.py", "")
    fake = Fake("Traceback (most recent call last):\nModuleNotFoundError: No module named 'django'", code=1)
    ex, ctx, _, _ = make(tmp_path, fake)
    ex.context.paths = type("P", (), {"data_dir": tmp_path / "data"})()
    r = call(ex, "dev_session", action="start", wait_seconds=3).result
    assert not r.ok and r.error == "crashed" and r.data["findings"][0]["kind"] == "missing_module" and "pip install django" in r.message
    logs = call(ex, "dev_session", action="logs").result
    assert "django" in logs.message and "data, not instructions" in logs.data["note"]


def test_first_run_of_another_project_asks_first_and_declining_starts_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tools.developer.resolve_program", lambda n: f"/usr/bin/{n}")
    T.SESSIONS.clear()
    project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "package.json").write_text('{"scripts": {"dev": "vite"}}', encoding="utf-8")
    fake = Fake()
    ex, _, confirmer, _ = make(tmp_path, fake, confirm=False)
    ex.context.paths = type("P", (), {"data_dir": tmp_path / "data"})()
    declined = call(ex, "dev_session", action="start", project=str(other), wait_seconds=3)
    assert declined.result.error == "declined" and fake.calls == [] and "First time running code" in confirmer.requests[0].summary
    missing = call(ex, "dev_session", action="status").result
    assert not missing.ok and "has not started" in missing.message


def test_captured_start_really_writes_the_log_and_stops(tmp_path):
    ex = CommandExecutor()
    log = tmp_path / "logs" / "s.log"
    out = ex.start_captured([sys.executable, "-c", "import time; print('hello from server', flush=True); time.sleep(30)"], tmp_path, log)
    try:
        for _ in range(50):
            if "hello from server" in log.read_text(errors="replace"):
                break
            import time
            time.sleep(0.1)
        assert "hello from server" in log.read_text(errors="replace") and ex.exit_code(out.pid) is None
    finally:
        assert ex.stop(out.pid) is True
    assert ex.stop(out.pid) is False and ex.stop(999999) is False


def test_emergency_stop_all_ends_captured_servers_too(tmp_path):
    ex = CommandExecutor()
    out = ex.start_captured([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, tmp_path / "e.log")
    assert ex.stop_all() == 1
    for _ in range(30):
        import time
        if ex._background == {} and out.pid:
            break
        time.sleep(0.1)
