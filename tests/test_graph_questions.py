"""Knowledge-graph questions: what is blocking a project, what is left open, what keeps failing, what is outdated."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.agent.shortcuts import match_shortcut
from app.config.settings import Settings
from app.core import knowledge_graph as kg
from app.tools.base import ToolContext
from app.tools.context_tools import KnowledgeArgs, ask_knowledge
from tests.test_v2 import db

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
ISO = NOW.isoformat(timespec="seconds")


def ctx_for(tmp_path: Path):
    return ToolContext(Settings(), database=db(tmp_path), clock=lambda: NOW)


def seed(ctx) -> None:
    with ctx.database.transaction() as conn:
        conn.execute("INSERT INTO missions (id, title, state, note, created_at, updated_at) VALUES "
                     "('m1', 'Ship the TMS release', 'paused', 'step 2 failed', ?, ?)", (ISO, ISO))
        conn.execute("INSERT INTO missions (id, title, state, created_at, updated_at) VALUES "
                     "('m2', 'Tidy desktop', 'completed', ?, ?)", (ISO, ISO))
        conn.execute("INSERT INTO mission_steps (mission_id, n, title, tool, arguments, state) VALUES "
                     "('m1', 1, 'build', 'run_command', '{}', 'done'), ('m1', 2, 'upload', 'run_command', '{}', 'failed')")
        conn.execute("INSERT INTO goals (title, status, progress, deadline, created_at, updated_at) VALUES "
                     "('Finish TMS login', 'active', 0.2, '2026-09-20', ?, ?)", (ISO, ISO))
        conn.execute("INSERT INTO todos (text, due, created_at) VALUES ('fix TMS login bug', '2026-09-25', ?)", (ISO,))
        conn.execute("INSERT INTO todos (text, due, created_at, depends_on) VALUES ('deploy TMS', NULL, ?, 1)", (ISO,))
        conn.execute("INSERT INTO todos (text, due, created_at) VALUES ('buy milk', '2026-10-01', ?)", (ISO,))
        for _ in range(3):
            conn.execute("INSERT INTO audit_log (ts, event, tool, details) VALUES (?, 'tool_result', 'open_website', ?)",
                         (ISO, json.dumps({"ok": False, "error": "offline"})))
        conn.execute("INSERT INTO audit_log (ts, event, tool, details) VALUES (?, 'tool_result', 'move_item', ?)",
                     (ISO, json.dumps({"ok": False, "error": "not_found"})))
        conn.execute("INSERT INTO audit_log (ts, event, tool, details) VALUES (?, 'tool_result', 'move_item', ?)",
                     (ISO, json.dumps({"ok": False, "error": "declined"})))  # a "no" from you is not a failure
        old = (NOW - timedelta(days=400)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO memories (content, category, source, created_at, updated_at) VALUES "
                     "('my phone is a Pixel 6', 'general', 'user', ?, ?)", (old, old))


def test_blockers_name_the_paused_mission_the_late_goal_and_the_waiting_todo(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    seed(ctx)
    answer, lines = kg.blockers(ctx, "TMS")
    text = " | ".join(lines)
    assert "mission \"Ship the TMS release\" is paused: step 2 failed" in text
    assert "goal \"Finish TMS login\" is past its deadline (2026-09-20)" in text
    assert "\"deploy TMS\" waits for \"fix TMS login bug\"" in text
    assert answer.startswith("3 thing(s) are holding back TMS") or answer.startswith("4 thing(s)")
    assert "buy milk" not in text and "Tidy desktop" not in text  # unrelated things are left out


def test_blockers_says_so_when_nothing_is_found_and_admits_its_limits(tmp_path: Path) -> None:
    answer, lines = kg.blockers(ctx_for(tmp_path), "Payroll")
    assert lines == [] and "I don't see anything blocking Payroll" in answer and "work done by hand" in answer


def test_what_is_still_open(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    seed(ctx)
    answer, lines = kg.unfinished(ctx)
    text = " | ".join(lines)
    assert "mission \"Ship the TMS release\" (paused): 1 step(s) left, next: upload" in text
    assert "to-do \"fix TMS login bug\" (due 2026-09-25)" in text and "buy milk" in text
    assert "Tidy desktop" not in text
    (tmp_path / "other").mkdir()
    assert "Nothing is left open" in kg.unfinished(ctx_for(tmp_path / "other"))[0]


def test_the_most_frequent_failure_ignores_declined_actions(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    seed(ctx)
    answer, lines = kg.recurring_failures(ctx)
    assert answer.startswith("The most frequent failure in the last 14 days: open_website (offline), 3 time(s)")
    assert "move_item: not_found x1" in lines and not any("declined" in line for line in lines)


def test_outdated_and_contradicting_memories(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    seed(ctx)
    answer, lines = kg.stale_or_conflicting(ctx)
    assert any("possibly outdated" in line and "Pixel 6" in line for line in lines)
    assert "need a look" in answer


def test_questions_are_routed_and_the_tool_answers(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    seed(ctx)
    for question, kind in (("what is blocking the TMS project?", "blocking"), ("what remains incomplete from my last "
                           "work session", "unfinished"), ("which recurring issue has caused the most failures", "failures"),
                           ("what information is outdated or contradictory", "stale")):
        result = ask_knowledge(KnowledgeArgs(question=question), ctx)
        assert result.ok and result.data["question_type"] == kind, question
        call = match_shortcut(question, lambda name: None)
        assert call is not None and call.name == "ask_knowledge", question
