from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.runs import Run, RunStore, running
from app.memory.database import Database
from app.tools.base import ToolContext, ToolError
from app.tools.diagnostics_tools import TraceArgs, trace_request
from app.utils.logging_setup import get_logger, log_event, setup_logging, teardown_logging
from app.config.settings import Settings


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    database.initialize()
    return database


def test_log_lines_carry_the_request_id(tmp_path: Path) -> None:
    log_file = setup_logging(tmp_path / "logs", "INFO")
    try:
        log = get_logger("trace_test")
        log_event(log, "outside")
        with running(Run("text", "hello", id="req-1")):
            log_event(log, "inside")
    finally:
        teardown_logging()
    lines = [json.loads(x) for x in log_file.read_text(encoding="utf-8").splitlines()]
    by_event = {r["fields"]["event"]: r["fields"] for r in lines if "event" in r.get("fields", {})}
    assert by_event["inside"]["trace_id"] == "req-1" and "trace_id" not in by_event["outside"]


def test_trace_request_collects_everything_for_one_run(db: Database) -> None:
    store = RunStore(db)
    old, newer = Run("voice", "open notepad", id="r-old"), Run("text", "what happened", id="r-new")
    with running(old, store):
        with db.transaction() as conn:
            conn.execute("INSERT INTO audit_log (ts, event, tool, permission_level, decision, details, run_id) "
                         "VALUES ('t', 'tool_call', 'open_application', 1, 'allowed', '{}', 'r-old')")
            conn.execute("INSERT INTO task_events (task_id, kind, ts, old_state, new_state, note, run_id) "
                         "VALUES ('m1', 'state', 't', 'planned', 'running', 'go', 'r-old')")
    with running(newer, store):
        ctx = ToolContext(Settings(), database=db)
        last = trace_request(TraceArgs(), ctx)  # "the last one" means the request before this one
        assert last.data["request"]["id"] == "r-old"
        assert [a["tool"] for a in last.data["actions"]] == ["open_application"]
        assert last.data["task_changes"][0]["new_state"] == "running"
        assert trace_request(TraceArgs(request_id="r-old"), ctx).ok
        with pytest.raises(ToolError):
            trace_request(TraceArgs(request_id="nope"), ctx)


def test_trace_with_no_history(db: Database) -> None:
    assert trace_request(TraceArgs(), ToolContext(Settings(), database=db)).data["steps"] == []
