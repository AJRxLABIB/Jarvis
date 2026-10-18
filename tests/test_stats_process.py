"""The process list is measured in a helper PROCESS, so it can't stall the window's animation (no shared GIL)."""

from __future__ import annotations

import sys
from pathlib import Path

from app.ui import stats_process
from app.ui.stats_process import StatsHelper

ROOT = Path(__file__).resolve().parents[1]
CHILD = [sys.executable, "-c", "import sys; sys.path.insert(0, %r); from app.ui.stats_process import run_stats_child; "
         "raise SystemExit(run_stats_child())" % str(ROOT)]


def test_helper_process_answers_and_closes(monkeypatch) -> None:
    monkeypatch.setattr(stats_process, "child_command", lambda: CHILD)
    helper = StatsHelper()
    rows = helper.busiest(4)
    assert isinstance(rows, list) and all(len(r) == 3 and isinstance(r[2], str) for r in rows)
    assert helper.process is not None and helper.process.pid != __import__("os").getpid()   # really another process
    assert isinstance(helper.busiest(4), list)                                              # the same helper again
    process = helper.process
    helper.close()
    assert process.poll() is not None


def test_a_helper_that_cannot_start_falls_back(monkeypatch) -> None:
    from app.ui.stats_sampler import take

    def broken(*a, **k):
        raise OSError("no")

    helper = StatsHelper(popen=broken)
    assert helper.busiest(4) is None and helper.failed
    sample = take(str(Path.cwd().anchor or "/"), True, helper)
    assert sample.processes is not None                       # measured in-thread instead: the numbers stay


def test_the_child_mode_runs_before_anything_else(monkeypatch) -> None:
    import pytest

    from app import main as app_main

    monkeypatch.setattr(stats_process, "run_stats_child", lambda: 0)
    monkeypatch.setattr(app_main, "bootstrap", lambda *a, **k: pytest.fail("no bootstrap for the helper"))
    assert app_main.main(["--stats-child"]) == 0
    cmd = stats_process.child_command()
    assert cmd[-1] == "--stats-child"


