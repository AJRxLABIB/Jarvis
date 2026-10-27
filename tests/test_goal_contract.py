"""Goal contracts: compile a request, check a plan against it, judge the real results as evidence."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.core import goal_contract as gc

TODAY = date(2026, 10, 27)      # a Tuesday


def c(text, **kw):
    return gc.compile(text, today=TODAY, **kw)


def test_open_and_remind_become_checkable_criteria():
    x = c("Open Chrome and remind me to call mum tomorrow at 9")
    assert [k.key for k in x.criteria] == ["open:chrome", "reminder"] and x.can_proceed
    assert x.deadline == date(2026, 10, 28)


def test_questions_only_when_a_guess_could_hurt():
    assert [q.why for q in c("delete old files").questions] == ["safety"]
    assert c(r"delete C:\Temp\old.log").can_proceed
    assert [q.why for q in c("remind me to drink water").questions] == ["correctness"]
    assert c("remind me to drink water at 5pm").can_proceed
    assert [q.why for q in c("send an email").questions] == ["privacy"]
    assert c("open notepad", projects=["A", "B"]).can_proceed            # a clear request is not questioned


def test_project_resolution_assumes_only_when_there_is_one():
    one = c("get my computer ready for development", projects=["TMS"])
    assert one.project == "TMS" and one.assumptions and one.can_proceed
    many = c("get my computer ready for development", projects=["TMS", "Jarvis"])
    assert not many.can_proceed and "Which project" in many.questions[0].text
    named = c("get my computer ready for development on Jarvis", projects=["TMS", "Jarvis"])
    assert named.project == "Jarvis" and named.can_proceed and not named.assumptions
    assert not c("finish my project", projects=[]).can_proceed


def test_required_vs_optional_criteria_for_development():
    x = c("make my computer ready for development", projects=["TMS"])
    assert [(k.key, k.required) for k in x.criteria] == [("workspace", True), ("workspace_checks", False)]


def test_deadlines():
    assert gc.parse_deadline("finish this week", TODAY) == date(2026, 10, 30)
    assert gc.parse_deadline("by friday", TODAY) == date(2026, 10, 30)
    assert gc.parse_deadline("by tuesday", TODAY) == date(2026, 11, 3)          # never "today" by accident
    assert gc.parse_deadline("by 2026-12-01", TODAY) == date(2026, 12, 1)
    assert gc.parse_deadline("by 2026-13-45", TODAY) is None
    assert gc.parse_deadline("sometime", TODAY) is None


def test_constraints_and_plan_check():
    x = c("prepare my workspace without installing anything, and don't delete anything")
    assert set(x.constraints) == {"no_install", "no_delete"}
    found = gc.check_plan(x, ["project_profile", "install_software", "delete_item"])
    assert len(found) == 2 and all(f.kind == "violates_constraint" for f in found)
    assert gc.check_plan(x, ["project_profile"]) == []
    assert gc.check_plan(c("do something"), ["delete_item"]) == []


def test_duplicates_and_busy_days():
    active = [gc.Active("to-do", "call mum about the visit"), *[gc.Active("to-do", f"t{i}", date(2026, 10, 28)) for i in range(3)]]
    x = c("remind me to call mum about the visit tomorrow", active=active)
    kinds = {k.kind for k in x.conflicts}
    assert kinds == {"duplicate", "busy_day"}
    assert not c("remind me to water the plants tomorrow at 8", active=[gc.Active("to-do", "call mum")]).conflicts
    assert any("past" in k.text for k in c("do it by 2020-01-01").conflicts)


def test_evidence_levels():
    E = gc.Evidence
    assert gc.judge("x", []) == gc.UNKNOWN
    assert gc.judge("x", [E("x", "hypothesis")]) == gc.TENTATIVE
    assert gc.judge("x", [E("x", "user")]) == gc.SUPPORTED
    assert gc.judge("x", [E("x", "document", False)]) == gc.CONTRADICTED
    assert gc.judge("x", [E("x", "verified", True, at="1")]) == gc.VERIFIED
    assert gc.judge("x", [E("x", "verified", True, at="1"), E("x", "observation", False, at="2")]) == gc.CONTRADICTED
    assert gc.judge("x", [E("x", "observation", False, at="1"), E("x", "verified", True, at="2")]) == gc.VERIFIED   # newest wins
    assert gc.judge("x", [E("x", "hypothesis"), E("x", "user", False)]) == gc.CONTRADICTED      # a guess never outvotes you
    assert gc.judge("other", [E("x", "verified")]) == gc.UNKNOWN


def test_a_model_claim_never_completes_a_goal():
    x = c("open notepad")
    assert gc.evaluate(x, [gc.Evidence("open:notepad", "hypothesis")]).verdict == "incomplete"
    assert gc.evaluate(x, [gc.Evidence("open:notepad", "user")]).verdict == "incomplete"      # strongly supported is not verified
    assert gc.evaluate(x, [gc.Evidence("open:notepad", "verified")]).verdict == "complete"


def test_goal_without_criteria_is_unverifiable_not_complete():
    done = gc.evaluate(c("help me finish my project", projects=["A"]), [])
    assert done.verdict == "unverifiable" and "can't verify" in done.describe()


def test_optional_criteria_do_not_block_completion():
    x = c("make my computer ready for development", projects=["A"])
    assert gc.evaluate(x, [gc.Evidence("workspace", "verified")]).verdict == "complete"


def step(n, tool, state, **arguments):
    return SimpleNamespace(n=n, tool=tool, state=state, arguments=arguments)


def test_evidence_from_mission_steps():
    x = c("open chrome and run the tests")
    ev = gc.evidence_from_steps(x, [step(1, "open_application", "done", name="Chrome"),
                                    step(2, "check_project", "failed"), step(3, "git_status", "pending")])
    done = gc.evaluate(x, ev)
    assert done.verdict == "contradicted"
    by = {r.criterion.key: r.status for r in done.results}
    assert by == {"open:chrome": gc.VERIFIED, "checks": gc.CONTRADICTED}
    assert gc.evaluate(x, gc.evidence_from_steps(x, [step(1, "open_application", "done", name="Firefox")])).verdict == "incomplete"
    assert gc.evidence_from_steps(x, [step(1, "open_application", "pending", name="chrome")]) == []     # never ran: says nothing


@pytest.mark.parametrize("text", ["", "   ", "???"])
def test_empty_requests_do_not_crash(text):
    x = c(text)
    assert x.criteria == [] and gc.evaluate(x, []).verdict == "unverifiable"


def test_goal_check_tool_end_to_end(tmp_path):
    import asyncio

    from app.ai.base import ToolCall
    from app.memory.database import Database
    from tests.test_v3210 import make

    database = Database(tmp_path / "g.db")
    database.initialize()
    ex, _, _ = make(database)
    result = asyncio.run(ex.execute(ToolCall("goal_check", {"request": "delete old files"}))).result
    assert result.ok and result.data["proceed"] is False and "Which files" in result.message
    nothing = asyncio.run(ex.execute(ToolCall("goal_check", {}))).result
    assert not nothing.ok and nothing.error == "not_found"
