from __future__ import annotations

from app.agent.focus import FocusSession
from app.agent.shortcuts import match_shortcut
from app.ai.base import ToolCall
from app.config.settings import Settings
from app.security.permissions import RiskLevel
from app.tools.base import ToolContext
from app.tools.focus import FocusArgs, _question, _risk, focus_mode


def session(**kw) -> FocusSession:
    s = FocusSession(30, started=1000.0)
    s.keep_closed = True
    s.blocked = ["Discord", "Epic Games Launcher", "Steam"]
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_running_blocked_apps_are_found_by_a_forgiving_match() -> None:
    s = session()
    assert s.guard_candidates(["chrome.exe", "Discord.exe", "EpicGamesLauncher.exe"], now=1100.0) == ["Discord", "Epic Games Launcher"]
    assert s.guard_candidates(["chrome.exe", "code.exe"], now=1100.0) == []


def test_nothing_is_closed_unless_keep_closed_was_asked_or_after_the_session() -> None:
    assert session(keep_closed=False).guard_candidates(["Discord.exe"], now=1100.0) == []
    assert session().guard_candidates(["Discord.exe"], now=1000.0 + 30 * 60 + 1) == []


def test_never_a_fight_at_most_three_times_and_not_twice_within_a_minute() -> None:
    s = session()
    s.note_reclosed("Discord", now=1100.0)
    assert s.guard_candidates(["Discord.exe"], now=1130.0) == []                      # too soon
    assert s.guard_candidates(["Discord.exe"], now=1170.0) == ["Discord"]            # a minute later: again
    s.note_reclosed("Discord", now=1170.0)
    s.note_reclosed("Discord", now=1300.0)
    assert s.reclosed["Discord"] == 3 and s.guard_candidates(["Discord.exe"], now=1500.0) == []   # three is the limit


def test_the_summary_reports_what_the_guard_did() -> None:
    s = session()
    s.note_reclosed("Discord")
    s.note_reclosed("Discord")
    text = s.summary(now=s.started + 25 * 60)
    assert "closed distracting apps again 2 times (Discord x2)" in text and "Nothing else came up" in text
    s.hold("Battery at 40%")
    assert "held back 1 note" in s.summary(now=s.started + 60)


def test_phrases_and_the_confirmation_mention_the_guard() -> None:
    call = match_shortcut("focus for 45 minutes and keep distracting apps closed", lambda n: None)
    assert call == ToolCall("focus_mode", {"action": "start", "minutes": 45, "close_distractions": True, "keep_closed": True})
    assert match_shortcut("focus for 45 minutes and close distractions", lambda n: None).arguments == {
        "action": "start", "minutes": 45, "close_distractions": True}
    args = FocusArgs(minutes=45, keep_closed=True)
    ctx = ToolContext(Settings())
    assert _risk(args, ctx) == RiskLevel.MODERATE and "close them again" in _question(args, ctx)
    assert _risk(FocusArgs(), ctx) == RiskLevel.SAFE


def test_the_tool_passes_keep_closed_to_the_window() -> None:
    calls = []
    ctx = ToolContext(Settings())
    ctx.focus = lambda *a, **k: calls.append((a, k)) or "ok"
    focus_mode(FocusArgs(minutes=30, keep_closed=True), ctx)
    focus_mode(FocusArgs(minutes=30, close_distractions=True), ctx)
    assert calls == [(("start", 30, True), {"keep_closed": True}), (("start", 30, True), {})]


def test_the_window_closes_returning_apps_and_keeps_watching(monkeypatch) -> None:
    pytest = __import__("pytest")
    pytest.importorskip("PySide6")
    import psutil

    from app.ui.controller_parts.background import BackgroundMixin

    class Fake(BackgroundMixin):
        def __init__(self) -> None:
            self._focus = session()
            self.closed: list[list[str]] = []
            self.scheduled = 0

        def _close_distractions(self, names):
            self.closed.append(names)
            return names

        def _schedule_focus_guard(self) -> None:
            self.scheduled += 1

    class P:
        def __init__(self, name: str) -> None:
            self.info = {"name": name}

    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [P("Discord.exe"), P("chrome.exe")])
    fake = Fake()
    fake._focus.started = __import__("time").monotonic()
    assert fake._focus_guard_tick() == ["Discord"] and fake.closed == [["Discord"]] and fake.scheduled == 1
    assert fake._focus_guard_tick() == [] and fake.scheduled == 2           # a minute has not passed: not closed again
    fake._focus = None
    assert fake._focus_guard_tick() == [] and fake.scheduled == 2           # the session ended: it stops watching
