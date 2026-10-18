from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.automation.tasks import AutomationStore, Step
from app.automation.triggers import ManualTrigger
from app.config.settings import Settings
from app.core import routines
from app.memory.database import Database
from app.memory.settings_store import SettingsStore
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from app.tools.routines_tool import RoutineArgs, routine_suggestions

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "j.db")
    database.initialize()
    return database


def log(db, day_offset: int, hour: int, minute: int, tool="open_application", arguments=None, source="voice", ok=True) -> None:
    at = (NOW - timedelta(days=day_offset)).replace(hour=hour, minute=minute)
    run = f"run-{day_offset}-{hour}-{minute}-{tool}"
    with db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO agent_runs (id, source, text, state, started_at) VALUES (?, ?, 'x', 'done', ?)",
                     (run, source, at.isoformat()))
        conn.execute("INSERT INTO audit_log (ts, event, tool, permission_level, decision, details, run_id) "
                     "VALUES (?, 'tool_result', ?, 1, 'allow', ?, ?)",
                     (at.isoformat(), tool, json.dumps({"ok": ok, "arguments": arguments or {"name": "Chrome"}, "message": "ok"}), run))


def found(db, **kw):
    return routines.detect(routines.load(db, NOW), **kw)


def test_the_same_action_at_the_same_time_on_three_days_is_a_routine(db) -> None:
    for day, minute in ((1, 2), (2, 10), (3, 0), (5, 5)):
        log(db, day, 8, minute)
    [r] = found(db)
    assert r.tool == "open_application" and r.arguments == {"name": "Chrome"} and r.time == "08:05" and r.occurrences == 4
    assert "around 08:05" in r.describe() and "4 different days" in r.describe()


def test_two_days_or_scattered_times_are_not_enough(db) -> None:
    log(db, 1, 8, 0)
    log(db, 2, 8, 5)
    assert found(db) == []
    for day, hour in ((3, 3), (4, 12), (5, 19)):                      # three days but at very different times
        log(db, day, hour, 0, arguments={"name": "Spotify"})
    assert found(db) == []


def test_only_your_own_requests_count_and_only_harmless_tools(db) -> None:
    for day in (1, 2, 3, 4):
        log(db, day, 8, 0, source="automation")                     # an automation did it: not a habit of yours
        log(db, day, 9, 0, tool="delete_item", arguments={"path": "x"})  # never a routine tool
        log(db, day, 10, 0, ok=False, arguments={"name": "Broken"})  # failures don't count
    assert found(db) == []


def test_weekday_pattern_and_old_history_ignored(db) -> None:
    for weekday in range(4):                                       # Mon-Thu of this week
        offset = (NOW.weekday() - weekday) % 7
        log(db, offset, 7, 30, arguments={"name": "Outlook"})
    log(db, 40, 7, 30, arguments={"name": "Old"})                   # outside the 21-day window
    log(db, 41, 7, 30, arguments={"name": "Old"})
    log(db, 42, 7, 30, arguments={"name": "Old"})
    [r] = found(db)
    assert r.arguments == {"name": "Outlook"} and r.days == (0, 1, 2, 3, 4) and "on weekdays" in r.when()


def test_argument_case_does_not_split_a_habit(db) -> None:
    for day, name in ((1, "Chrome"), (2, "chrome"), (3, " CHROME ")):
        log(db, day, 8, 0, arguments={"name": name})
    [r] = found(db)
    assert r.occurrences == 3


def test_dismissed_and_already_automated_ones_are_not_suggested(db) -> None:
    for day in (1, 2, 3):
        log(db, day, 8, 0)
    [r] = found(db)
    kv = SettingsStore(db)
    routines.dismiss(kv, r.key)
    assert found(db, skip_keys=routines.dismissed(kv)) == []
    store = AutomationStore(db)
    store.save("morning", ManualTrigger(), [Step(type="tool", tool="open_application", arguments={"name": "chrome"})], None)
    assert r.key in routines.automated_keys(store)


# ------------------------------------------------------------------------------- the tool


def ctx(db) -> ToolContext:
    return ToolContext(Settings(), database=db, clock=lambda: NOW)


def test_tool_lists_dismisses_and_accepts(db) -> None:
    c = ctx(db)
    assert "haven't noticed" in routine_suggestions(RoutineArgs(), c).message
    for day in (1, 2, 3):
        log(db, day, 8, 0)
    listed = routine_suggestions(RoutineArgs(), c)
    key = listed.data["routines"][0]["key"]
    assert key in listed.message and "automate" in listed.message
    with pytest.raises(ToolError):
        routine_suggestions(RoutineArgs(action="accept"), c)
    with pytest.raises(ToolError):
        routine_suggestions(RoutineArgs(action="accept", key="nonexistent"), c)
    routine_suggestions(RoutineArgs(action="dismiss", key=key), c)
    assert routine_suggestions(RoutineArgs(), c).data["routines"] == []


def test_accepting_makes_a_real_automation_with_the_usual_checks(db) -> None:
    c = ctx(db)
    c.get_tool_registry = build_default_registry     # the precheck validates the steps against the real registry
    for day in (1, 2, 3):
        log(db, day, 8, 0)
    key = routine_suggestions(RoutineArgs(), c).data["routines"][0]["key"]
    result = routine_suggestions(RoutineArgs(action="accept", key=key), c)
    assert result.ok and "Routine: open application 08:00" in result.message
    [task] = [t for t in AutomationStore(db).all() if t.name.startswith("Routine:")]
    assert task.steps[0].tool == "open_application" and task.trigger.time == "08:00"
    assert routine_suggestions(RoutineArgs(), c).data["routines"] == []      # already automated: not suggested again


def test_accept_is_confirmed_and_list_is_safe() -> None:
    from app.security.permissions import RiskLevel
    from app.tools.routines_tool import _risk

    assert _risk(RoutineArgs(action="accept", key="x"), None) == RiskLevel.MODERATE
    assert _risk(RoutineArgs(), None) == RiskLevel.SAFE and _risk(RoutineArgs(action="dismiss", key="x"), None) == RiskLevel.SAFE
