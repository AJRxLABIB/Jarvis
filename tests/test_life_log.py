"""Journal / voice notes, habit tracker, expense trend + CSV export, calendar conflicts, the knowledge-graph browser
helpers, the life-log dialog, and `run.py --restart`."""

from __future__ import annotations

import asyncio
import csv
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.memory.database import Database


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "x.db")
    database.initialize()
    return database


def _ctx(db: Database, tmp_path: Path, now: datetime | None = None):
    from app.config.settings import Settings
    from app.tools.base import ToolContext

    ctx = ToolContext(settings=Settings(data_dir=tmp_path), database=db)
    if now is not None:
        ctx.clock = lambda: now
    return ctx


# ------------------------------------------------------------------ journal
def test_journal_add_search_today_and_voice_source(db: Database, tmp_path: Path) -> None:
    from app.core import runs
    from app.core.journal import Journal, guess_mood
    from app.tools.personal_tools import JournalArgs, journal

    assert guess_mood("Feeling tired after the exam") == "tired" and guess_mood("meeting at 5") == ""
    book = Journal(db)
    first = book.add("Finished the TMS login page, feeling great")
    assert book.recent(1)[0]["mood"] == "great" and book.recent(1)[0]["id"] == first
    with pytest.raises(ValueError):
        book.add("   ")
    assert [r["id"] for r in book.search("tms LOGIN")] == [first] and book.search("100%_") == []
    ctx = _ctx(db, tmp_path)
    run = runs.Run("voice", "note to self", taint=None, user_words=frozenset())
    with runs.running(run):
        spoken = asyncio.run(_call(journal, JournalArgs(action="add", text="Call the bank tomorrow"), ctx))
    assert spoken.ok and spoken.data["source"] == "voice" and "voice note" in spoken.message
    today = journal(JournalArgs(action="today"), ctx)
    assert "Call the bank" in today.message and today.data["entries"][0]["source"] == "voice"
    assert journal(JournalArgs(action="delete", entry_id=first), ctx).ok
    assert book.search("tms") == []


async def _call(handler, args, ctx):
    return handler(args, ctx)


def test_journal_and_habit_phrases_become_direct_commands() -> None:
    from app.agent.shortcuts import match_journal_habits as m

    assert m("note to self: buy printer ink").arguments == {"action": "add", "text": "buy printer ink"}
    assert m("Add to my journal: today was long but good").arguments["text"] == "today was long but good"
    assert m("add finished chapter 3 to my diary").arguments["text"] == "finished chapter 3"
    assert m("show my journal for today").arguments == {"action": "today"}
    assert m("track the habit drink water").arguments == {"action": "add", "name": "drink water"}
    assert m("mark habit exercise done").arguments == {"action": "done", "name": "exercise"}
    assert m("I did my habit reading today").arguments == {"action": "done", "name": "reading"}
    assert m("which habits are left today").arguments == {"action": "today"}
    assert m("my habit streaks").arguments == {"action": "streaks"}
    assert m("mark buy milk done") is None                                  # a to-do, not a habit: left to the model
    assert m("open the journal app") is None


# ------------------------------------------------------------------ habits
def test_streaks_pure() -> None:
    from app.core.habit_tracker import streaks

    today = date(2026, 10, 17)
    days = {today - timedelta(days=k) for k in (1, 2, 3, 6, 7)}
    assert streaks(days, today) == (3, 3)                                   # today not done yet: yesterday counts
    assert streaks(days | {today}, today) == (4, 4)
    assert streaks({today - timedelta(days=2)}, today) == (0, 1)
    assert streaks(set(), today) == (0, 0)


def test_habit_tracker_and_tool(db: Database, tmp_path: Path) -> None:
    from app.core.habit_tracker import HabitTracker
    from app.tools.base import ToolError
    from app.tools.personal_tools import HabitArgs, habits

    now = datetime(2026, 10, 17, 21, 0)
    ctx = _ctx(db, tmp_path, now)
    assert "aren't tracking" in habits(HabitArgs(action="today"), ctx).message
    assert habits(HabitArgs(action="add", name="Drink Water"), ctx).ok
    assert "already tracked" in habits(HabitArgs(action="add", name="drink water"), ctx).message
    habits(HabitArgs(action="add", name="exercise"), ctx)
    tracker = HabitTracker(db)
    tracker.check("exercise", now.date() - timedelta(days=1))
    done = habits(HabitArgs(action="done", name="exercise"), ctx)
    assert done.data["streak"] == 2 and "Streak: 2 days" in done.message
    assert "Already marked" in habits(HabitArgs(action="done", name="exercise"), ctx).message
    assert habits(HabitArgs(action="today"), ctx).message == "Still to do today: drink water."
    assert "exercise 2 (best 2)" in habits(HabitArgs(action="streaks"), ctx).message
    assert "exercise 2/7" in habits(HabitArgs(action="report"), ctx).message
    with pytest.raises(ToolError):
        habits(HabitArgs(action="done", name="juggling"), ctx)
    grid = tracker.grid(now.date(), 3)
    assert grid["exercise"] == [False, True, True] and grid["drink water"] == [False, False, False]
    assert habits(HabitArgs(action="remove", name="exercise"), ctx).ok
    assert "exercise" not in tracker.grid(now.date())
    tracker.add("exercise")                                                 # comes back with its history
    assert tracker.grid(now.date(), 2)["exercise"] == [True, True]


# ------------------------------------------------------------------ expenses: trend + CSV export
def test_expense_trend_and_safe_csv_export(db: Database, tmp_path: Path) -> None:
    from app.core.finance import Finance
    from app.tools.finance_tool import ExpenseArgs, expenses

    finance = Finance(db)
    now = datetime.now().astimezone()
    finance.add(300, "lunch", when=now)
    finance.add(120, "=HYPERLINK(\"http://evil\")", category="other", when=now)
    last_month = (now.replace(day=1) - timedelta(days=3))
    finance.add(900, "rent share", category="bills", when=last_month)
    months = finance.monthly_totals(3, now)
    assert len(months) == 3 and months[-1] == (now.strftime("%b"), 420.0) and months[-2][1] == 900.0
    path = tmp_path / "out" / "e.csv"
    assert finance.export_csv(path) == 2
    rows = list(csv.reader(path.open(encoding="utf-8-sig")))
    assert rows[0] == ["id", "date", "amount", "currency", "category", "note"]
    assert any(r[5].startswith("'=HYPERLINK") for r in rows[1:])              # no formula runs in Excel
    assert finance.export_csv(tmp_path / "all.csv", all_months=True) == 3
    ctx = _ctx(db, tmp_path)
    ctx.paths = SimpleNamespace(data_dir=tmp_path)
    result = expenses(ExpenseArgs(action="export"), ctx)
    assert result.ok and Path(result.data["path"]).is_file() and result.data["count"] == 2
    assert "Last 6 months" in expenses(ExpenseArgs(action="trend"), ctx).message


# ------------------------------------------------------------------ calendar conflicts
def test_calendar_conflicts_per_day(db: Database, tmp_path: Path, monkeypatch) -> None:
    from app.tools import outlook
    from app.tools.base import ToolResult
    from app.tools.personal_tools import ConflictArgs, calendar_conflicts, find_conflicts

    ref = datetime(2026, 10, 17, 9, 0)
    events = [{"subject": "Standup", "start": "Mon 19 Oct 10:00", "end": "Mon 19 Oct 10:30"},
              {"subject": "Dentist", "start": "Mon 19 Oct 10:15", "end": "Mon 19 Oct 11:00"},
              {"subject": "Gym", "start": "Tue 20 Oct 10:15", "end": "Tue 20 Oct 11:00"}]
    found = find_conflicts(events, ref)
    assert len(found) == 1 and found[0].startswith("Mon 19 Oct") and "Standup" in found[0] and "Dentist" in found[0]
    monkeypatch.setattr(outlook, "calendar_events", lambda args, ctx: ToolResult.success("x", events=events))
    result = calendar_conflicts(ConflictArgs(), _ctx(db, tmp_path, ref))
    assert result.data["conflicts"] == found and result.data["checked"] == 3
    monkeypatch.setattr(outlook, "calendar_events", lambda args, ctx: ToolResult.success("x", events=events[2:]))
    assert "No overlapping" in calendar_conflicts(ConflictArgs(day="today"), _ctx(db, tmp_path, ref)).message


def test_new_tools_are_registered_routed_and_respect_autonomy() -> None:
    from app.agent.tool_router import KEYWORDS
    from app.security import autonomy
    from app.tools.catalog import build_default_registry

    registry = build_default_registry()
    for name in ("journal", "habits", "calendar_conflicts", "expenses"):
        assert registry.get(name) is not None
    assert "habit" in KEYWORDS["personal"] and "double booked" in KEYWORDS["office"]
    assert autonomy.blocked_in("observe", "calendar_conflicts") is None
    assert autonomy.blocked_in("observe", "journal", {"action": "search"}) is None
    assert autonomy.blocked_in("observe", "journal", {"action": "add"}) is not None
    assert autonomy.blocked_in("observe", "expenses", {"action": "trend"}) is None
    assert autonomy.blocked_in("observe", "expenses", {"action": "export"}) is not None


# ------------------------------------------------------------------ knowledge-graph browser
def _graph():
    from app.core.knowledge_graph import Graph, Node

    g = Graph()
    for nid, kind, name in (("project:tms", "project", "TMS"), ("technology:laravel", "technology", "laravel"),
                            ("file:a", "file", "routes.php"), ("decision:1", "decision", "use MySQL")):
        g.add(Node(nid, kind, name))
    g.link("project:tms", "uses", "technology:laravel")
    g.link("project:tms", "contains", "file:a")
    g.link("decision:1", "about", "project:tms")
    g.link("decision:1", "about", "technology:laravel")
    return g


def test_graph_ego_layout() -> None:
    from app.ui.graph_browser import ego

    around = ego(_graph(), "project:tms")
    assert [nid for nid, _, _ in around] == ["technology:laravel", "decision:1", "file:a"]   # most linked first, then by name
    assert dict((n, r) for n, r, _ in around)["technology:laravel"] == "uses"
    angles = [a for _, _, a in around]
    assert len(set(round(a, 6) for a in angles)) == 3
    assert ego(_graph(), "file:a", limit=5) == [("project:tms", "contains", pytest.approx(-1.5707963))]


def test_graph_browser_and_life_log_widgets(qapp, db: Database, tmp_path: Path) -> None:
    from app.core.finance import Finance
    from app.core.habit_tracker import HabitTracker
    from app.ui.graph_browser import GraphBrowser
    from app.ui.personal_dialog import PersonalDialog

    browser = GraphBrowser(SimpleNamespace(), graph=_graph())
    assert browser.items.count() == 4
    browser.go("project:tms")
    browser.view.resize(500, 400)
    browser.view.grab()                                                     # paints, records clickable spots
    assert len(browser.view._spots) == 3 and "TMS" in browser.details.text()
    browser.go("technology:laravel")
    browser.back()
    assert browser.history[-1] == "project:tms"
    browser.search.setText("laravel")
    assert browser.items.count() == 1

    HabitTracker(db).add("exercise")
    Finance(db).add(250, "coffee")
    ctx = _ctx(db, tmp_path)
    ctx.paths = SimpleNamespace(data_dir=tmp_path)
    dialog = PersonalDialog(ctx, today=date.today())
    assert dialog.tabs.count() == 4 and dialog.habit_table.rowCount() == 1
    dialog._toggle_cell(0, dialog.DAYS - 1)                                 # tick today
    assert HabitTracker(db).habits(date.today())[0].done_today
    dialog.entry.setPlainText("A quiet productive day")
    dialog.add_entry()
    assert "quiet productive" in dialog.entries.item(0).text()
    assert dialog.month_chart.bars[-1][1] == 250.0
    dialog.grab()
    dialog.close()


# ------------------------------------------------------------------ run.py --restart
def test_restart_request_is_taken_once_and_stale_ones_ignored(tmp_path: Path) -> None:
    from app.core import relaunch

    assert not relaunch.take_request(tmp_path)
    relaunch.request_restart(tmp_path)
    assert relaunch.take_request(tmp_path) and not relaunch.take_request(tmp_path)
    (tmp_path / relaunch.RESTART_REQUEST).write_text(json.dumps({"at": time.time() - 3600}), encoding="utf-8")
    assert not relaunch.take_request(tmp_path) and not (tmp_path / relaunch.RESTART_REQUEST).exists()
    lock = tmp_path / "x.lock"
    lock.write_text("4242\njarvis\nhost\n", encoding="utf-8")
    assert relaunch.lock_pid(lock) == 4242 and relaunch.lock_pid(tmp_path / "none") is None


def test_cli_restart_paths(tmp_path: Path, monkeypatch) -> None:
    import psutil

    from app.core import relaunch

    started = []
    lock = tmp_path / "lock"
    # nothing running -> just start
    ok, msg = relaunch.cli_restart(tmp_path, lock, start=lambda: started.append(1))
    assert ok and "wasn't running" in msg and started == [1]
    # running Jarvis that takes the request and restarts itself -> we must NOT start a second copy
    lock.write_text("999999\n", encoding="utf-8")
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(relaunch, "is_jarvis", lambda pid: True)
    monkeypatch.setattr(relaunch, "_is_background_host", lambda pid: False)

    def window_takes_it(pid, wait):
        assert relaunch.take_request(tmp_path)
        return True

    ok, msg = relaunch.cli_restart(tmp_path, lock, start=lambda: started.append(2), waiter=window_takes_it)
    assert ok and msg == "Jarvis restarted." and started == [1]
    # an unrelated process holding a recycled PID is never touched
    monkeypatch.setattr(relaunch, "is_jarvis", lambda pid: False)
    ok, _ = relaunch.cli_restart(tmp_path, lock, start=lambda: started.append(3), waiter=lambda p, w: pytest.fail())
    assert ok and started == [1, 3]


def test_restart_flag_exists() -> None:
    from app.main import build_parser

    assert build_parser().parse_args(["--restart"]).restart
