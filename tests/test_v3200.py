"""3.20.0: proactive intelligence - signals, ranking, levels, quiet hours, limits, repeats, explanations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import proactive
from app.core.proactive import ENGINE, Candidate, in_quiet_hours, parse_quiet_hours
from app.memory.database import Database
from app.security.permissions import PermissionEngine
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry

NOON = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc).astimezone().replace(hour=12)


def cand(key="k", urgency=0.9, importance=0.8, **kw) -> Candidate:
    return Candidate(key, kw.pop("category", "goal"), "Title", "Message", urgency, importance, **kw)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "j.db")
    database.initialize()
    return database


def test_scores_and_levels() -> None:
    important, trivial = cand("a"), cand("b", urgency=0.2, importance=0.2, interruption=0.8)
    assert important.score > 0.75 > trivial.score
    assert ENGINE.decide(important, Settings(proactive_level="minimal"), NOON).outcome == "speak"
    decision = ENGINE.decide(trivial, Settings(proactive_level="normal"), NOON)
    assert decision.outcome == "drop" and "below your 'normal' level" in decision.reason
    assert ENGINE.decide(cand("c"), Settings(proactive_level="off"), NOON).outcome == "drop"


def test_urgent_alerts_pass_even_when_off_unless_disabled() -> None:
    urgent = cand("battery", category="system", emergency=True, urgency=1.0)
    assert ENGINE.decide(urgent, Settings(proactive_level="off"), NOON).outcome == "speak"
    ENGINE.clear()
    assert ENGINE.decide(urgent, Settings(proactive_level="off", proactive_emergency_exceptions=False),
                         NOON).outcome == "drop"


def test_no_repeats_within_six_hours() -> None:
    settings = Settings()
    assert ENGINE.decide(cand("same"), settings, NOON).outcome == "speak"
    assert ENGINE.decide(cand("same"), settings, NOON + timedelta(hours=1)).outcome == "drop"
    assert ENGINE.decide(cand("same"), settings, NOON + timedelta(hours=7)).outcome == "speak"


def test_quiet_hours_and_hourly_limit_mean_notification_center_only() -> None:
    night = NOON.replace(hour=23, minute=30)
    assert ENGINE.decide(cand("n"), Settings(quiet_hours="23:00-07:00"), night).outcome == "notify"
    settings = Settings(proactive_max_per_hour=2)
    outcomes = [ENGINE.decide(cand(f"x{i}"), settings, NOON + timedelta(minutes=i)).outcome for i in range(3)]
    assert outcomes == ["speak", "speak", "notify"]


def test_switched_off_categories() -> None:
    assert ENGINE.decide(cand(category="project"), Settings(proactive_categories="goal, mission"),
                         NOON).outcome == "drop"


@pytest.mark.parametrize(("text", "start", "end", "at", "quiet"), [
    ("22:00-07:00", "22:00", "07:00", "23:15", True), ("22:00-07:00", "22:00", "07:00", "12:00", False),
    ("13-14", "13:00", "14:00", "13:30", True)])
def test_quiet_hour_parsing(text, start, end, at, quiet) -> None:
    parsed = parse_quiet_hours(text)
    assert (parsed[0].strftime("%H:%M"), parsed[1].strftime("%H:%M")) == (start, end)
    hour, minute = map(int, at.split(":"))
    assert in_quiet_hours(NOON.replace(hour=hour, minute=minute), parsed) is quiet
    assert parse_quiet_hours("") is None and not in_quiet_hours(NOON, None)


def test_signals_from_goals_missions_and_failures(db) -> None:
    import json

    from app.core.goals import GoalStore

    store = GoalStore(db)
    store.add("Submit the thesis", deadline=(NOON.date() - timedelta(days=2)).isoformat(), priority=1)
    store.add("Finish TMS login", deadline=NOON.date().isoformat())
    old = store.add("Learn Rust")
    with db.transaction() as conn:
        conn.execute("UPDATE goals SET updated_at = ? WHERE id = ?", ((NOON - timedelta(days=10)).isoformat(), old.id))
        for _ in range(6):
            conn.execute("INSERT INTO audit_log (ts, event, tool, details) VALUES (?, 'tool_result', "
                         "'brightness_control', ?)", (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                      json.dumps({"ok": False, "error": "unsupported"})))
    found = proactive.candidates(ToolContext(Settings(), database=db), NOON)
    keys = {c.key.split(":")[0] for c in found}
    assert {"goal-overdue", "goal-due", "goal-stale", "failing"} <= keys
    overdue = next(c for c in found if c.key.startswith("goal-overdue"))
    assert "was due 2 day(s) ago" in overdue.message and overdue.score >= 0.75


def test_uncommitted_work_signal(monkeypatch) -> None:
    from app.core import context_engine
    from app.core.context_engine import ContextSnapshot, Fact

    snap = ContextSnapshot(0, {"project": Fact("TMS", "t"), "git_changes": Fact(14, "git")})
    monkeypatch.setattr(context_engine.ENGINE, "history", lambda: [snap])
    found = proactive._project_signals(None)
    assert found and "14 uncommitted changes" in found[0].message and found[0].action == "git status"


def test_background_delivery_speaks_or_only_notifies(db, monkeypatch) -> None:
    from app.core import events
    from app.ui.controller_parts.background import BackgroundMixin

    spoken, published = [], []
    monkeypatch.setattr(events.BUS, "publish", lambda name, **data: published.append((name, data)))
    fake = SimpleNamespace(_busy=False, tts=SimpleNamespace(is_speaking=False), _pending_confirmation=None,
                           ctx=SimpleNamespace(database=db), executor=SimpleNamespace(
                               context=ToolContext(Settings(proactive_level="high"), database=db)),
                           speak_up=lambda message, priority, event, **data: spoken.append(message))
    from app.core.goals import GoalStore

    GoalStore(db).add("Submit the thesis", deadline=(NOON.date() - timedelta(days=1)).isoformat(), priority=1)
    delivered = BackgroundMixin.proactive_tick(fake, now=NOON)
    assert delivered and spoken and "Submit the thesis" in spoken[0]
    assert BackgroundMixin.proactive_tick(fake, now=NOON + timedelta(minutes=10)) == []  # not repeated


def test_proactive_control_tool(db) -> None:
    changed = {}
    ctx = ToolContext(Settings(proactive_level="normal"), database=db,
                      apply_setting=lambda field, value: changed.update({field: value}))
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx)

    def run(**arguments):
        return asyncio.run(ex.execute(ToolCall("proactive_control", arguments))).result

    assert run(action="more").data["level"] == "active" and changed["proactive_level"] == "active"
    assert run(action="less").data["level"] == "minimal"
    assert run(action="quiet_hours", hours="22:00-06:30").ok and changed["quiet_hours"] == "22:00-06:30"
    assert not run(action="quiet_hours", hours="whenever").ok
    ENGINE.decide(cand("w"), Settings(), NOON)
    why = run(action="why")
    assert "urgency 0.9" in why.message and "speak" in why.message
    assert "Proactive level: normal" in run(action="status").message


@pytest.mark.parametrize(("text", "arguments"), [
    ("be more proactive", {"action": "more"}), ("be less proactive", {"action": "less"}),
    ("stop suggesting things", {"action": "set_level", "level": "off"}), ("why did you suggest that", {"action": "why"}),
    ("set quiet hours from 10 pm to 7 am", {"action": "quiet_hours", "hours": "22:00-07:00"}),
])
def test_proactive_shortcuts(text, arguments) -> None:
    call = match_shortcut(text, lambda n: None)
    assert call is not None and call.name == "proactive_control" and call.arguments == arguments
