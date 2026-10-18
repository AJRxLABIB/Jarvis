"""Master prompt 4 (round 36): notification de-duplication and retention, the security center's live state, and the
configurable daily briefing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import events
from app.core.notifications import NotificationCenter, connect
from app.memory.database import Database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "x.db")
    database.initialize()
    return database


def test_same_unread_notice_is_not_added_twice(db):
    center = NotificationCenter(db)
    first = center.add("Battery low", "12% left", source="t", category="alert")
    assert center.add("Battery low", "12% left", source="t", category="alert") == first and center.duplicate
    center.mark_read([first])
    assert center.add("Battery low", "12% left", source="t", category="alert") != first      # read: tell again
    assert center.add("Battery low", "9% left", source="t", category="alert")              # different text: new
    assert len(center.items()) == 3


def test_event_bus_shows_a_repeated_event_once(db):
    bus, center, shown = events.EventBus(), NotificationCenter(db), []
    connect(bus, center, on_new=lambda *a: shown.append(a))
    for _ in range(3):
        bus.publish(events.BATTERY_LOW, message="10% left")
    assert len(shown) == 1 and center.unread_count() == 1


def test_old_read_notices_are_removed(db):
    center = NotificationCenter(db)
    old = center.add("Old", "x", source="t", category="system")
    center.mark_read([old])
    stale = (datetime.now(timezone.utc) - timedelta(days=NotificationCenter.RETENTION_DAYS + 1)).isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.execute("UPDATE notifications SET ts = ? WHERE id = ?", (stale, old))
    keep = center.add("Unread old", "y", source="t", category="system")
    with db.transaction() as conn:
        conn.execute("UPDATE notifications SET ts = ? WHERE id = ?", (stale, keep))
    center.add("New", "z", source="t", category="system")
    titles = [n.title for n in center.items(include_archived=True)]
    assert "Old" not in titles and "Unread old" in titles                            # unread ones wait for you


def test_security_center_right_now(db):
    from app.config.settings import Settings
    from app.core import security_center

    live = {"mic_muted": False, "hands_free": True, "recording": False, "screen_capture": True,
            "sharing_phones": ["Pixel"], "remote": "https://192.168.0.5:8765/", "pending_approval": "Delete 3 files?",
            "mode": "normal"}
    sections = security_center.collect(db, Settings(), live=live)
    assert sections[0].title == "Right now"
    text = security_center.describe(sections)
    assert "listening for the wake word" in text and "Pixel is sharing" in text and "Delete 3 files?" in text
    assert any(i.level == "warn" and "approval" in i.text for i in sections[0].items)
    assert security_center.collect(db, Settings())[0].title != "Right now"           # no window: settings only


def test_live_privacy_state_from_the_controller():
    from app.ui.controller_parts.features import FeaturesMixin

    host = FeaturesMixin.__new__(FeaturesMixin)
    host.ctx = SimpleNamespace(settings=SimpleNamespace(screen_capture_enabled=False))
    host.mic_muted, host.hands_free, host._listening = True, None, False
    host._pending_confirmation = None
    state = host.live_privacy_state()
    assert state["mic_muted"] and not state["hands_free"] and state["pending_approval"] == "" and state["mode"]


def test_briefing_sections_and_length():
    from app.tools.assistant import BRIEFING_SECTIONS, briefing_sections

    s = SimpleNamespace(briefing_length="normal", briefing_sections="")
    assert "notifications" not in briefing_sections("", "", s) and "weather" in briefing_sections("", "", s)
    assert briefing_sections("short", "", s) == {"reminders", "todos", "missions"}
    assert briefing_sections("full", "", s) == set(BRIEFING_SECTIONS)
    assert briefing_sections("", "weather, todos, nonsense", s) == {"weather", "todos"}
    assert briefing_sections("", "", SimpleNamespace(briefing_length="full", briefing_sections="battery")) == {"battery"}


def test_briefing_mentions_an_unfinished_mission(db):
    from app.config.settings import Settings
    from app.core.missions import MissionStore, Step
    from app.tools.assistant import BriefingArgs, daily_briefing
    from app.tools.base import ToolContext

    MissionStore(db).create("tidy the downloads", [Step(1, "look", "look", {})])
    ctx = ToolContext(Settings(), database=db, clock=lambda: datetime(2026, 10, 19, 8, 0))
    result = asyncio.run(daily_briefing(BriefingArgs(length="short"), ctx))
    assert "Unfinished mission: 'tidy the downloads'" in result.message and "weather" not in result.data["sections"]
