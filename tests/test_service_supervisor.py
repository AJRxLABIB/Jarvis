from __future__ import annotations

import json
from pathlib import Path

from app.automation import supervisor as sv


class Child:
    def __init__(self, codes: list[int | None]) -> None:
        self.codes = codes          # successive poll() results; the last is the exit code
        self.returncode: int | None = None
        self.terminated = self.killed = False

    def poll(self):
        if len(self.codes) > 1 or self.codes[0] is None:
            code = self.codes.pop(0) if len(self.codes) > 1 else None
        else:
            code = self.codes[0]
        self.returncode = code
        return code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None) -> int:
        if getattr(self, "stubborn", False):
            raise TimeoutError
        return 0


def run(children: list[Child], stop_after_waits: int, tmp: Path, clock=None):
    waits: list[float] = []
    log: list[str] = []
    it = iter(children)

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        return len(waits) >= stop_after_waits

    s = sv.Supervisor(lambda: next(it), wait, data_dir=tmp, log=log.append, clock=clock or (lambda: 0.0))
    s.run()
    return s, waits, log


def test_a_normal_exit_is_not_restarted(tmp_path: Path) -> None:
    s, waits, log = run([Child([0])], stop_after_waits=2, tmp=tmp_path)
    assert s.starts == 1 and any("not restarting" in m for m in log)


def test_a_locked_lock_retries_after_a_minute(tmp_path: Path) -> None:
    s, waits, _ = run([Child([3]), Child([0])], stop_after_waits=99, tmp=tmp_path)
    assert s.starts == 2 and waits[0] == sv.RETRY_LOCKED_SECONDS


def test_crashes_back_off_then_give_up(tmp_path: Path) -> None:
    kids = [Child([1]) for _ in range(10)]
    s, waits, log = run(kids, stop_after_waits=50, tmp=tmp_path)
    assert waits[:4] == [5.0, 10.0, 20.0, 40.0]
    assert s.starts == sv.MAX_CRASHES and any("giving up" in m for m in log)


def test_old_crashes_do_not_count(tmp_path: Path) -> None:
    now = iter(range(0, 100000, 1000))        # each crash 1000 s apart: always outside the 10 minute window
    s, waits, _ = run([Child([1]) for _ in range(8)] + [Child([0])], stop_after_waits=99, tmp=tmp_path,
                      clock=lambda: float(next(now)))
    assert set(waits[:8]) == {5.0}            # every crash is "the first": no escalation, no give-up


def test_stopping_the_service_asks_the_child_to_stop_first(tmp_path: Path) -> None:
    child = Child([None])
    s, _, log = run([child], stop_after_waits=1, tmp=tmp_path)
    assert json.loads((tmp_path / "background.control").read_text()) == {"command": "stop"}
    assert not child.terminated and any("stopping" in m for m in log)


def test_a_stubborn_child_is_terminated_then_killed(tmp_path: Path) -> None:
    child = Child([None])
    child.stubborn = True
    run([child], stop_after_waits=1, tmp=tmp_path)
    assert child.terminated and child.killed


def test_the_command_is_an_argument_list_using_the_project_interpreter(tmp_path: Path) -> None:
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_text("")
    cmd = sv.service_command(tmp_path)
    assert cmd == [str(scripts / "pythonw.exe"), str(tmp_path / "run.py"), "--background"]


def test_the_service_glue_does_not_widen_anything() -> None:
    text = (Path(__file__).resolve().parents[1] / "packaging" / "jarvis_service.py").read_text(encoding="utf-8")
    assert "subprocess" not in text and "Popen" not in text      # all process code lives in supervisor.py
