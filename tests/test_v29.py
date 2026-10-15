"""2.9: what was I doing, continue where I left off, save this setup, routines, screen questions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import activity
from app.agent.habits import routine_question
from app.agent.shortcuts import RESUME, SAVE_SETUP, match_shortcut
from app.agent.tool_router import categories_for
from app.ai.base import ToolCall
from app.memory.database import Database

TZ = timezone(timedelta(hours=6))  # Dhaka
NOW = datetime(2026, 9, 24, 15, 0, tzinfo=TZ)


def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    database.initialize()
    return database


def log(database: Database, at: datetime, tool: str, ok: bool = True, **arguments) -> None:
    details = {"arguments": arguments, "ok": ok, "error": None if ok else "failed", "message": f"did {tool}"}
    with database.transaction() as conn:
        conn.execute("INSERT INTO audit_log (ts, event, tool, details) VALUES (?, 'tool_result', ?, ?)",
                     (at.astimezone(timezone.utc).isoformat(timespec="seconds"), tool, json.dumps(details)))


def day(hour: int, minute: int = 0, days_ago: int = 0) -> datetime:
    return (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=minute)


# ------------------------------------------------------------------ periods

@pytest.mark.parametrize("phrase, label, start, end", [
    ("before lunch", "this morning", day(5), day(13)),
    ("yesterday", "yesterday", day(0, days_ago=1), day(0)),
    ("yesterday evening", "yesterday evening", day(17, days_ago=1), day(0)),
    ("last 2 hours", "in the last 2 hours", NOW - timedelta(hours=2), NOW),
    ("today", "today", day(0), NOW),
])
def test_periods(phrase, label, start, end) -> None:
    assert activity.period(phrase, NOW) == (start, end, label)


def test_unknown_period() -> None:
    assert activity.period("when I was a child", NOW) is None


# ------------------------------------------------------------------ what was I doing

def test_summary_from_the_real_log(tmp_path: Path) -> None:
    database = db(tmp_path)
    log(database, day(9, 10), "open_application", name="Visual Studio Code")
    log(database, day(9, 12), "open_website", site="YouTube")
    log(database, day(9, 30), "open_application", ok=False, name="WordShop")  # failed: not something you did
    log(database, day(11, 40), "play_music", query="kk songs")
    log(database, day(14, 0), "open_application", name="Steam")  # after lunch
    start, end, label = activity.period("before lunch", NOW)
    text = activity.summarize(activity.load(database, start, end), label)
    assert text == ("This morning, between 09:10 and 11:40, I opened Visual Studio Code; went to YouTube; "
                    "played kk songs.")
    assert "WordShop" not in text and "Steam" not in text


def test_nothing_recorded_says_so_honestly(tmp_path: Path) -> None:
    text = activity.summarize([], "yesterday")
    assert "didn't do anything for you yesterday" in text and "by hand" in text


def test_recall_tool(tmp_path: Path) -> None:
    from app.tools.activity import RecallArgs, recall_activity

    database = db(tmp_path)
    log(database, day(10), "open_application", name="Chrome")
    ctx = SimpleNamespace(database=database, clock=lambda: NOW)
    result = recall_activity(RecallArgs(period="this morning"), ctx)
    assert result.ok and "opened Chrome" in result.message and result.data["actions"] == 1


# ------------------------------------------------------------------ continue where I left off

def test_last_session_and_what_to_reopen(tmp_path: Path) -> None:
    database = db(tmp_path)
    # yesterday evening's session
    log(database, day(20, 0, 1), "open_application", name="Visual Studio Code")
    log(database, day(20, 5, 1), "open_application", name="Google Chrome", profile="Night")
    log(database, day(20, 6, 1), "open_website", site="GitHub")
    log(database, day(20, 30, 1), "open_application", name="Steam")
    log(database, day(20, 45, 1), "close_application", name="Steam")  # closed again: not reopened
    log(database, day(20, 50, 1), "open_application", name="Visual Studio Code")  # twice: once
    # an older session, long before
    log(database, day(9, 0, 1), "open_application", name="Canva")
    session = activity.last_session(database, NOW)
    calls = activity.reopen_calls(session)
    assert calls == [ToolCall("open_application", {"name": "Visual Studio Code"}),
                     ToolCall("open_application", {"name": "Google Chrome", "profile": "Night"}),
                     ToolCall("open_website", {"site": "GitHub"})]


def test_the_session_going_on_now_is_not_the_one_to_resume(tmp_path: Path) -> None:
    database = db(tmp_path)
    log(database, day(9, 0), "open_application", name="Word")
    log(database, day(14, 50), "open_application", name="Spotify")  # 10 minutes ago: now
    assert [a.arguments["name"] for a in activity.last_session(database, NOW)] == ["Word"]
    assert [a.arguments["name"] for a in activity.current_session(database, NOW)] == ["Spotify"]


def test_resume_by_voice_reopens_through_the_executor(tmp_path: Path, monkeypatch) -> None:
    from tests.test_agent_loop import build, run

    conv, _, executions = build([])
    database = db(tmp_path)
    log(database, datetime.now(TZ) - timedelta(hours=20), "open_website", site="YouTube")
    monkeypatch.setattr(conv.tools.context, "database", database)
    conv._clock = lambda: datetime.now(TZ)
    reply, _ = run(conv, "continue where I left off")
    assert [e.call for e in executions] == [ToolCall("open_website", {"site": "YouTube"})]


def test_resume_with_nothing_recorded_explains(tmp_path: Path, monkeypatch) -> None:
    from tests.test_agent_loop import build, run

    conv, _, executions = build([])
    monkeypatch.setattr(conv.tools.context, "database", db(tmp_path))
    reply, _ = run(conv, "pick up where we left off")
    assert executions[0].call.name == "recall_activity" and "didn't do anything" in reply.text


# ------------------------------------------------------------------ save this setup

def test_save_this_setup_as_a_workflow(tmp_path: Path, monkeypatch) -> None:
    from tests.test_agent_loop import build, run

    conv, _, executions = build([], confirm=False)  # you're asked first; here: no
    database = db(tmp_path)
    now = datetime.now(TZ)
    log(database, now - timedelta(minutes=20), "open_application", name="Visual Studio Code")
    log(database, now - timedelta(minutes=19), "open_website", site="GitHub")
    monkeypatch.setattr(conv.tools.context, "database", database)
    conv._clock = lambda: datetime.now(TZ)
    run(conv, "save this as my work setup")
    saved = executions[0]
    assert saved.call.name == "save_workflow" and saved.call.arguments["name"] == "work setup"
    assert saved.call.arguments["steps"] == [{"tool": "open_application", "arguments": {"name": "Visual Studio Code"}},
                                             {"tool": "open_website", "arguments": {"site": "GitHub"}}]
    assert saved.result.error == "declined"  # saving a workflow always asks first


# ------------------------------------------------------------------ voice phrases

@pytest.mark.parametrize("said, period", [
    ("what was I doing before lunch?", "before lunch"),
    ("what did I do yesterday", "yesterday"),
    ("what did you do for me this morning", "this morning"),
    ("what was i doing", "today"),
    ("গতকাল আমি কী করেছি?", "yesterday"),
    ("দুপুরের আগে কী করছিলাম", "before lunch"),
])
def test_recall_phrases(said, period) -> None:
    call = match_shortcut(said, lambda n: None)
    assert call == ToolCall("recall_activity", {"period": period})


@pytest.mark.parametrize("said", ["continue where I left off", "pick up where we left off", "restore my last session",
                                  "আগের কাজ আবার খোলো"])
def test_resume_phrases(said) -> None:
    assert RESUME.match(said)


@pytest.mark.parametrize("said", ["continue", "where did I leave my keys", "open my last file"])
def test_not_resume(said) -> None:
    assert not RESUME.match(said)


def test_save_setup_phrases() -> None:
    assert SAVE_SETUP.match("save this as my work setup").group("name") == "work"
    assert SAVE_SETUP.match("remember these apps as gaming").group("name") == "gaming"
    assert SAVE_SETUP.match("save the file") is None


# ------------------------------------------------------------------ routines and screen questions

def test_routine_question() -> None:
    assert routine_question(["VS Code"]) == "You usually open VS Code around now. Shall I open it?"
    assert routine_question(["VS Code", "Chrome", "Spotify"], "sir") == (
        "You usually open VS Code, Chrome and Spotify around now, sir. Shall I open them?")


def test_accepting_a_routine_opens_every_app() -> None:
    from app.agent.shortcuts import split_commands

    assert split_commands("open " + " and ".join(["VS Code", "Chrome", "Spotify"])) == [
        "open VS Code", "open Chrome", "open Spotify"]


@pytest.mark.parametrize("said", ["what does this error mean", "summarise this page", "explain this"])
def test_screen_questions_get_the_screen_tools(said) -> None:
    assert "vision" in categories_for(said)
