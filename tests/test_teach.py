"""Teach by showing: only what Jarvis really did is noted; secrets, risky and forbidden tools are left out."""

from __future__ import annotations

from app.automation.teach import Teacher
from app.security.permissions import RiskLevel


def test_records_only_during_a_session_and_skips_unsafe_things():
    t = Teacher()
    t.record("open_application", {"name": "chrome"}, RiskLevel.SAFE)          # no session: nothing kept
    session = t.start("morning")
    t.record("open_application", {"name": "chrome"}, RiskLevel.SAFE)
    t.record("set_volume", {"level": 30}, RiskLevel.MODERATE)
    t.record("send_email", {"to": "a@b.c"}, RiskLevel.DANGEROUS)
    t.record("delete_file", {"path": "x"}, RiskLevel.DANGEROUS)
    t.record("save_workflow", {"name": "x"}, RiskLevel.MODERATE)
    t.record("open_url", {"url": "https://x.test/?token=sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD"}, RiskLevel.SAFE)
    assert [s["tool"] for s in session.steps] == ["open_application", "set_volume"]
    assert len(session.skipped) == 4
    assert t.cancel() is session and not t.active


def test_a_session_holds_at_most_ten_steps():
    t = Teacher()
    session = t.start("many")
    for n in range(14):
        t.record("open_application", {"name": f"app{n}"}, RiskLevel.SAFE)
    assert len(session.steps) == 10 and len(session.skipped) == 4


def test_the_executor_feeds_the_teacher_and_stop_asks_then_saves(tmp_path):
    from app.automation.tasks import AutomationStore
    from app.automation.teach import TEACHER
    from app.memory.database import Database
    from tests.test_automation import run_tool

    db = Database(tmp_path / "a.db")
    db.initialize()
    TEACHER.cancel()
    try:
        assert run_tool(db, "teach_workflow", action="start", name="demo flow")[0].result.ok
        assert run_tool(db, "get_datetime")[0].result.ok                     # a real tool run through the executor
        assert TEACHER.session is not None and [s["tool"] for s in TEACHER.session.steps] == ["get_datetime"]
        declined, asked = run_tool(db, "teach_workflow", confirm=False, action="stop")
        assert not declined.result.ok and "demo flow" in asked.requests[0].summary and not AutomationStore(db).all()
        saved, _ = run_tool(db, "teach_workflow", action="stop")
        assert saved.result.ok and not TEACHER.active
        assert AutomationStore(db).find("demo flow").steps[0].tool == "get_datetime"
    finally:
        TEACHER.cancel()
