from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agent.shortcuts import match_shortcut
from app.config.settings import Settings
from app.core import history_qa
from app.memory.database import Database
from app.tools.base import ToolContext, ToolError
from app.tools.history_tool import HistoryArgs, ask_history

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)      # a Thursday


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "j.db")
    database.initialize()
    return database


def log(db, days_ago: float, tool: str, message: str, arguments=None, ok=True, text="") -> int:
    at = NOW - timedelta(days=days_ago)
    run = f"run-{tool}-{days_ago}"
    with db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO agent_runs (id, source, text, state, started_at) VALUES (?, 'voice', ?, 'done', ?)",
                     (run, text, at.isoformat()))
        return conn.execute("INSERT INTO audit_log (ts, event, tool, permission_level, decision, details, run_id) "
                            "VALUES (?, 'tool_result', ?, 1, 'allow', ?, ?)",
                            (at.isoformat(), tool, json.dumps({"ok": ok, "arguments": arguments or {}, "message": message}), run)).lastrowid


def test_keywords_reduce_words_to_stems_and_drop_filler() -> None:
    assert history_qa.keywords("When did I last run the backups?") == ["run", "backup"]
    assert history_qa.keywords("how many times did I open Spotify") == ["open", "spotify"]
    assert history_qa.keywords("I backed up my files") == ["backup", "file"]
    assert history_qa.keywords("the of and") == []


def test_last_time_names_the_day_and_the_record(db) -> None:
    log(db, 9, "create_backup", "Backup finished (older)")
    newest = log(db, 2, "create_backup", "Backup finished, 31 files")
    log(db, 1, "open_application", "Opened Chrome", {"name": "Chrome"})
    text, matches = history_qa.answer_last(db, "when did I last run the backup", NOW)
    assert "Tuesday at 12:00 PM (2 days ago)" in text and f"audit #{newest}" in text and "31 files" in text
    assert "only what Jarvis recorded" in text and matches[0].source == f"audit #{newest}"


def test_arguments_count_as_evidence(db) -> None:
    log(db, 0.1, "open_application", "Opened it", {"name": "Spotify"})
    text, _ = history_qa.answer_last(db, "when did I last open spotify", NOW)
    assert "today at" in text and "open application" in text


def test_nothing_found_is_said_plainly_and_failures_do_not_count(db) -> None:
    log(db, 1, "create_backup", "Backup failed", ok=False)
    text, matches = history_qa.answer_last(db, "when did I last run the backup", NOW)
    assert "can't find anything" in text and matches == []
    assert "Tell me what to look for" in history_qa.answer_last(db, "when did I", NOW)[0]


def test_counting_within_a_period(db) -> None:
    for days in (0.2, 1.5, 3, 20):
        log(db, days, "open_application", "Opened Spotify", {"name": "Spotify"})
    since = history_qa.week_start(NOW)                                      # Monday 12 Oct
    text, matches = history_qa.answer_count(db, "how many times did I open spotify", NOW, since, None, "this week")
    assert text.startswith("3 records") and "on 3 days" in text and len(matches) == 3
    assert history_qa.answer_count(db, "how many times did I open zoom", NOW, since, None, "this week")[0].startswith("I find 0")


def test_finished_todos_are_answerable(db) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO todos (text, done, done_at, created_at) VALUES ('write the report', 1, ?, 'x')",
                     ((NOW - timedelta(days=3)).isoformat(),))
    text, _ = history_qa.answer_last(db, "when did I finish the report", NOW)
    assert "finished the to-do \"write the report\"" in text and "to-do #" in text


def ctx(db) -> ToolContext:
    return ToolContext(Settings(), database=db, clock=lambda: NOW)


def test_tool_handles_periods_and_unknown_periods(db) -> None:
    log(db, 0.2, "open_application", "Opened Spotify", {"name": "Spotify"})
    log(db, 5, "open_application", "Opened Spotify", {"name": "Spotify"})
    today = ask_history(HistoryArgs(kind="count", what="open spotify", period="today"), ctx(db))
    assert today.message.startswith("1 record") and today.data["records"][0]["source"].startswith("audit #")
    week = ask_history(HistoryArgs(kind="count", what="open spotify", period="this week"), ctx(db))
    assert week.message.startswith("1 record")
    with pytest.raises(ToolError):
        ask_history(HistoryArgs(what="open spotify", period="in the year 1500"), ctx(db))


def test_shortcuts_only_catch_things_jarvis_records() -> None:
    call = match_shortcut("when did I last run the backup", lambda n: None)
    assert call is not None and call.name == "ask_history" and call.arguments == {"kind": "last", "what": "run the backup"}
    count = match_shortcut("how many times did I open spotify this week", lambda n: None)
    assert count.arguments == {"kind": "count", "what": "open spotify", "period": "this week"}
    assert match_shortcut("when did I last visit the dentist", lambda n: None) is None
    assert match_shortcut("how many times did I laugh today", lambda n: None) is None
