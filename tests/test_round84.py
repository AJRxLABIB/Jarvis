import asyncio

import pytest

from app.core import demo_record as DR, screen_history as SH
from app.memory.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.initialize()
    return d


# ---- screen history ---------------------------------------------------------------------------------------------------
def test_should_capture_rules():
    assert SH.should_capture("A", "A", 5) is False                    # too soon
    assert SH.should_capture("B", "A", 25) is True                    # the window changed
    assert SH.should_capture("A", "A", 60) is False                   # same window, not yet the long gap
    assert SH.should_capture("A", "A", 200) is True


def test_store_dedupe_search_purge_delete(db):
    st = SH.Store(db)
    assert st.add(1000.0, "Chrome", "Build log", "ERROR: connection refused on port 5432 while starting postgres")
    assert st.add(1010.0, "Chrome", "Build log", "ERROR: connection refused on port 5432 while starting postgres") is False
    assert st.add(1020.0, "Code", "main.py", "short") is False
    assert st.add(2000.0, "Code", "main.py - editor", "def handler(request): return response with status 200")
    hits = st.search("connection refused")
    assert len(hits) == 1 and hits[0]["title"] == "Build log" and "5432" in hits[0]["snippet"]
    assert st.search("editor handler")[0]["app"] == "Code" and st.search("zzz") == [] and st.search(" ") == []
    assert st.search("connection", since=1500.0) == []
    assert st.stats()["rows"] == 2
    assert st.purge(1, now=1000.0 + 86400 + 500) == 1 and st.stats()["rows"] == 1
    assert st.delete_all() == 1 and st.stats()["rows"] == 0


def test_recorder_ticks_privacy_and_indicator(db):
    st = SH.Store(db)
    t = {"v": 1000.0}
    title = {"v": "Notes"}
    shot = {"v": SH.Shot("Notepad", "Notes", "meeting notes about the quarterly budget review")}
    lights = []

    async def read():
        return shot["v"]

    rec = SH.Recorder(st, lambda: title["v"], read, clock=lambda: t["v"], indicator=lights.append)

    async def go():
        assert await rec.tick() == "saved"
        assert await rec.tick() == "wait"                             # too soon
        t["v"] += 30
        assert await rec.tick() == "wait"                             # same window, under the long gap
        title["v"] = "Bank"
        shot["v"] = SH.Shot(blocked="Banking and payments zone is active")
        assert await rec.tick() == "blocked"
        t["v"] += 30
        title["v"] = "Notes"
        shot["v"] = SH.Shot("Notepad", "Notes", "meeting notes about the quarterly budget review")
        assert await rec.tick() == "same"                             # nothing new on screen: no second row

        async def boom():
            raise RuntimeError("ocr died")

        rec.read = boom
        t["v"] += 30
        title["v"] = "Other"
        assert await rec.tick() == "error"

    asyncio.run(go())
    assert (rec.saved, rec.blocked, rec.errors) == (1, 1, 1) and st.stats()["rows"] == 1 and "Banking" in rec.last_block
    text = SH.describe(st.stats(), rec, allowed=False, days=7)
    assert "switched off in .env" in text and "no pictures" in text


def test_recorder_loop_stops_when_disabled_and_lights(db):
    lights, on = [], {"v": True}

    async def read():
        on["v"] = False                                               # the setting was switched off during the session
        return SH.Shot("A", "T", "some long enough text on the screen")

    async def nosleep(_):
        return None

    rec = SH.Recorder(SH.Store(db), lambda: "T", read, enabled=lambda: on["v"], indicator=lights.append, sleep=nosleep, clock=lambda: 5000.0)

    async def go():
        rec.start()
        await rec.task

    asyncio.run(go())
    assert lights == [True, False] and rec.saved == 1


def test_screen_history_tool_gates(db):
    from app.security.permissions import RiskLevel
    from app.tools import capture_tools as CT
    from app.tools.base import ToolError
    from app.tools.catalog import build_default_registry

    t = build_default_registry().get("screen_history")
    assert t.level_for(CT.HistoryArgs(action="search", query="x"), None) == RiskLevel.SAFE
    assert t.level_for(CT.HistoryArgs(action="start"), None) == RiskLevel.MODERATE and t.level_for(CT.HistoryArgs(action="delete_all"), None) == RiskLevel.MODERATE
    S = type("S", (), {"screen_history_enabled": False, "screen_capture_enabled": True, "screen_history_days": 7})()
    ctx = type("C", (), {"database": db, "settings": S})()
    with pytest.raises(ToolError, match="Screen history is off"):
        asyncio.run(CT.screen_history(CT.HistoryArgs(action="start"), ctx))
    out = asyncio.run(CT.screen_history(CT.HistoryArgs(action="search", query="anything"), ctx))
    assert "nothing stored" in out.message
    assert "not recording" in asyncio.run(CT.screen_history(CT.HistoryArgs(), ctx)).message


def test_settings_default_off():
    from app.config.settings import Settings

    f = Settings.model_fields
    assert f["screen_history_enabled"].default is False and f["screen_history_days"].default == 7


# ---- click demonstration ----------------------------------------------------------------------------------------------
def test_session_drops_password_zone_unnamed_own_and_repeats():
    s = DR.Session("r", 0.0)
    zone = lambda p, t: "Banking zone" if "bank" in t.lower() else None        # noqa: E731
    assert s.add(DR.Click("notepad.exe", "Notes", "File"), zone) == "kept"
    assert s.add(DR.Click("notepad.exe", "Notes", "File"), zone) == "repeated click"
    assert s.add(DR.Click("chrome.exe", "Login", "Password", password=True), zone) == "password field"
    assert s.add(DR.Click("chrome.exe", "My Bank", "Transfer"), zone) == "privacy zone"
    assert s.add(DR.Click("app.exe", "X", "  "), zone) == "control has no name"
    assert s.add(DR.Click("python.exe", "Jarvis", "Send", ours=True), zone) == "on Jarvis itself"
    assert s.add(DR.Click("notepad.exe", "Notes", "Save"), zone) == "kept"
    steps = DR.to_steps(s)
    assert steps == [{"tool": "open_application", "arguments": {"name": "notepad"}}, {"tool": "press_control", "arguments": {"name": "File"}},
                     {"tool": "press_control", "arguments": {"name": "Save"}}]
    text = DR.describe(s, steps)
    assert "password field" in text and "Keys were not recorded" in text and "Password" not in text.replace("password field", "")


def test_step_limit_and_steps_are_valid_tools():
    from app.automation.tasks import parse_steps
    from app.tools.catalog import build_default_registry

    s = DR.Session("r", 0.0)
    for i in range(DR.MAX_STEPS + 5):
        s.add(DR.Click("a.exe", "T", f"Button {i}"))
    assert len(s.clicks) == DR.MAX_STEPS and s.dropped["over the step limit"] == 5
    reg = build_default_registry()
    steps = DR.to_steps(DR.Session("r", 0.0, [DR.Click("notepad.exe", "N", "Save")]))
    assert all(reg.get(x["tool"]) is not None for x in steps) and len(parse_steps(steps)) == 2


def test_poller_records_press_edges_only_and_times_out():
    s = DR.Session("r", 0.0)
    states = iter([False, True, True, False, True, False, False])
    clicks = iter([DR.Click("a.exe", "T", "One"), DR.Click("a.exe", "T", "Two")])
    t = {"v": 0.0}

    def pressed():
        try:
            return next(states)
        except StopIteration:
            t["v"] = 999.0                                            # then the time limit ends the session
            return False

    p = DR.ClickPoller(s, pressed, lambda: next(clicks), clock=lambda: t["v"], sleep=lambda _: None, max_seconds=300)
    p.run()
    assert [c.name for c in s.clicks] == ["One", "Two"] and p.ended == "time limit"   # a held button is one click

    def bad():
        raise OSError("no element")

    s2 = DR.Session("r", 0.0)
    st = iter([True, False])
    p2 = DR.ClickPoller(s2, lambda: next(st, False) or (p2._stop.set() or False), bad, sleep=lambda _: None)
    p2.run()
    assert s2.clicks == [] and p2.ended == "stopped"


def test_demo_and_history_cannot_run_inside_automations():
    from app.automation.tasks import FORBIDDEN_STEP_TOOLS

    assert {"demo_record", "screen_history"} <= FORBIDDEN_STEP_TOOLS


def test_demo_tool_levels_and_empty_states():
    from app.security.permissions import RiskLevel
    from app.tools import capture_tools as CT
    from app.tools.base import ToolError
    from app.tools.catalog import build_default_registry

    t = build_default_registry().get("demo_record")
    assert t.level_for(CT.DemoArgs(action="status"), None) == RiskLevel.SAFE and t.level_for(CT.DemoArgs(action="start", name="x"), None) == RiskLevel.MODERATE
    DR.ACTIVE = DR.LAST = None
    assert "no draft" in CT.demo_record(CT.DemoArgs(), None).message
    with pytest.raises(ToolError):
        CT.demo_record(CT.DemoArgs(action="save", name="x"), None)
    with pytest.raises(ToolError):
        CT.demo_record(CT.DemoArgs(action="stop"), None)
