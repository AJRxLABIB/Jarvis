"""3.0: focus mode, richer briefing, files by content, personal recognition hints, answer length,
Bangla corrections, HUD motion."""

from __future__ import annotations

import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.conversation import answer_detail
from app.agent.focus import FocusSession, app_list, clamp_minutes
from app.agent.shortcuts import match_shortcut
from app.ai.base import ToolCall
from app.config.settings import Settings
from app.tools.base import ToolContext

TZ = timezone(timedelta(hours=6))


# ------------------------------------------------------------------ focus mode

@pytest.mark.parametrize("said, arguments", [
    ("focus for 45 minutes", {"action": "start", "minutes": 45}),
    ("start focus mode", {"action": "start", "minutes": 25}),
    ("focus for an hour and close distractions", {"action": "start", "minutes": 60, "close_distractions": True}),
    ("focus for half an hour", {"action": "start", "minutes": 30}),
    ("do not disturb for 2 hours", {"action": "start", "minutes": 120}),
    ("৪৫ মিনিট মনোযোগ মোড চালু করো", {"action": "start", "minutes": 45}),
    ("stop focus", {"action": "stop"}),
    ("মনোযোগ বন্ধ করো", {"action": "stop"}),
    ("how much time is left on my focus", {"action": "status"}),
])
def test_focus_commands(said, arguments) -> None:
    assert match_shortcut(said, lambda n: None) == ToolCall("focus_mode", arguments)


@pytest.mark.parametrize("said", ["focus on the text", "how long is left", "focus the chrome window"])
def test_not_focus_commands(said) -> None:
    found = match_shortcut(said, lambda n: None)
    assert found is None or found.name != "focus_mode"


def test_focus_session_holds_notes_and_sums_up() -> None:
    session = FocusSession(30, started=1000.0)
    assert session.left_minutes(now=1000.0 + 600) == 20 and not session.over(now=1600.0)
    session.hold("Disk C is 95% full.")
    session.hold("Disk C is 95% full.")  # once
    session.hold("Your download finished: report.pdf")
    text = session.summary(now=1000.0 + 1800)
    assert text == ("Focus time is over: 30 minutes. I held back 2 notes: Disk C is 95% full; "
                    "Your download finished: report.pdf.")
    assert FocusSession(25, started=0.0).summary(now=60.0).endswith("Nothing came up while you worked.")
    assert clamp_minutes(1) == 5 and clamp_minutes(999) == 240
    assert app_list("Steam; Discord, ") == ["Steam", "Discord"]


def test_focus_tool_needs_confirmation_only_to_close_apps() -> None:
    from app.security.permissions import RiskLevel
    from app.tools.focus import FocusArgs, _risk, focus_mode

    ctx = ToolContext(Settings())
    assert _risk(FocusArgs(action="start", minutes=30), ctx) == RiskLevel.SAFE
    assert _risk(FocusArgs(action="start", close_distractions=True), ctx) == RiskLevel.MODERATE
    seen = []
    ctx.focus = lambda action, minutes, close: seen.append((action, minutes, close)) or "ok"
    assert focus_mode(FocusArgs(action="start", minutes=45), ctx).message == "ok" and seen == [("start", 45, False)]


def test_controller_holds_alerts_during_focus() -> None:
    from app.ui.controller import AssistantController

    spoken, shown = [], []
    fake = SimpleNamespace(_focus=FocusSession(30), notice=SimpleNamespace(emit=shown.append),
                           tts=SimpleNamespace(speak=spoken.append))
    fake.speak_up = lambda *a, **k: AssistantController.speak_up(fake, *a, **k)
    AssistantController._alert(fake, "Download finished.")
    AssistantController._alert(fake, "Battery critically low: 5%.", urgent=True)
    assert fake._focus.held == ["Download finished."] and spoken == ["Battery critically low: 5%."]


# ------------------------------------------------------------------ briefing

class Items(list):
    IncludeRecurrences = False

    def Sort(self, *a):  # noqa: N802
        pass

    def Restrict(self, query):  # noqa: N802
        return Items(i for i in self if "UnRead" not in query or getattr(i, "UnRead", False))

    @property
    def Count(self):  # noqa: N802
        return len(self)


def test_briefing_adds_meetings_and_unread_mail() -> None:
    from app.tools.assistant import _outlook_lines

    when = datetime(2026, 9, 24, 10, 30)
    namespace = SimpleNamespace(GetDefaultFolder=lambda n: SimpleNamespace(Items=Items(
        [SimpleNamespace(Subject="Thesis meeting", Start=when, End=when, Location="DIU")] if n == 9 else
        [SimpleNamespace(UnRead=True), SimpleNamespace(UnRead=True), SimpleNamespace(UnRead=False)])))
    ctx = ToolContext(Settings(), clock=lambda: datetime(2026, 9, 24, 8, 0).astimezone())
    assert _outlook_lines(ctx, namespace) == ["1 meeting today; the first is 'Thesis meeting' at 10:30.",
                                              "2 unread emails."]


def test_briefing_never_starts_outlook(monkeypatch) -> None:
    from app.tools import assistant

    monkeypatch.setattr(assistant, "_outlook_running", lambda: False)
    assert assistant._outlook_lines(ToolContext(Settings())) == []


def test_briefing_mentions_last_session_in_the_morning(tmp_path: Path) -> None:
    from app.memory.database import Database
    from app.tools.assistant import _last_session_line
    from tests.test_v29 import log

    database = Database(tmp_path / "t.db")
    database.initialize()
    morning = datetime(2026, 9, 24, 8, 0, tzinfo=TZ)
    log(database, morning - timedelta(hours=11), "open_application", name="Visual Studio Code")
    log(database, morning - timedelta(hours=11, minutes=-2), "open_website", site="GitHub")
    ctx = SimpleNamespace(database=database)
    line = _last_session_line(ctx, morning)
    assert line == ("Last time you were working with Visual Studio Code and GitHub; say 'continue where I left off' "
                    "to open them again.")
    assert _last_session_line(ctx, morning.replace(hour=15)) == ""  # only in the morning


# ------------------------------------------------------------------ files by content

def write_docx(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
                                              "</w:body></w:document>")


def test_find_by_what_the_file_is_about(tmp_path: Path) -> None:
    from app.tools.filesystem import _by_content

    thesis = tmp_path / "final_v3.docx"
    write_docx(thesis, "Chapter 1: my thesis on travel planning with local AI")
    bill = tmp_path / "scan001.txt"
    bill.write_text("Electricity bill for August, DESCO", encoding="utf-8")
    named = tmp_path / "thesis notes.md"
    named.write_text("nothing inside", encoding="utf-8")
    found = [(os.path.getmtime(p), p, p.stat().st_size) for p in (thesis, bill, named)]
    about_thesis = [p.name for _, p, _ in _by_content(found, "my thesis")]
    assert about_thesis == ["thesis notes.md", "final_v3.docx"]  # the name counts most, then the text
    assert [p.name for _, p, _ in _by_content(found, "electricity bill")] == ["scan001.txt"]
    assert _by_content(found, "holiday photos") == []


def test_find_files_accepts_about_alone() -> None:
    from app.tools.filesystem import FindArgs

    args = FindArgs.model_validate({"about": "thesis", "extension": "pdf", "modified_within_days": 7})
    assert args.about == "thesis"


# ------------------------------------------------------------------ recognition

def test_personal_words_for_the_recognizer(tmp_path: Path) -> None:
    from app.agent.aliases import AliasStore
    from app.agent.habits import record
    from app.cli_voice import personal_words
    from app.memory.database import Database

    database = Database(tmp_path / "t.db")
    database.initialize()
    AliasStore(tmp_path).set("wordshop", "app", "Photoshop")
    now = datetime(2026, 9, 24, 9, 0)
    for _ in range(3):
        record(database, "Visual Studio Code", now)
    record(database, "Spotify", now)
    ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), database=database)
    words = personal_words(ctx)
    assert words[:4] == ["wordshop", "Photoshop", "Visual Studio Code", "Spotify"]


@pytest.mark.parametrize("said, fix", [
    ("না, আমি ফটোশপ বলেছি", "ফটোশপ"),
    ("না, ফটোশপ বলেছি", "ফটোশপ"),
    ("no, I meant Photoshop", "Photoshop"),
])
def test_corrections_in_both_languages(said, fix) -> None:
    from app.voice.corrections import parse_correction

    assert parse_correction(said) == fix


# ------------------------------------------------------------------ answers

@pytest.mark.parametrize("said, detail", [
    ("tell me more", "long"), ("explain that in detail", "long"), ("বিস্তারিত বলো", "long"),
    ("in one line please", "short"), ("সংক্ষেপে বলো", "short"), ("what time is it", ""),
])
def test_answer_detail(said, detail) -> None:
    assert answer_detail(said) == detail


def test_prompt_follows_the_detail_and_asks_for_web_facts() -> None:
    from app.ai.prompts import build_system_prompt

    tools = [SimpleNamespace(name="web_search", description="", parameters={}),
             SimpleNamespace(name="calculate", description="", parameters={})]
    now = datetime(2026, 9, 24, 9, 0)
    long = build_system_prompt(assistant_name="Jarvis", now=now, tools=tools, spoken=True, detail="long")
    assert "more detail" in long and "read aloud: keep it to one or two" not in long
    assert "use web_search instead of answering from memory" in long and "calculate tool" in long
    short = build_system_prompt(assistant_name="Jarvis", now=now, spoken=True, detail="short")
    assert "one sentence" in short and "web_search" not in short  # no search tool offered: not mentioned
    offline = build_system_prompt(assistant_name="Jarvis", now=now, tools=tools, offline_mode=True)
    assert "use web_search" not in offline


# ------------------------------------------------------------------ HUD motion

def test_tween_glides_and_stops() -> None:
    from app.ui import motion

    tween = motion.Tween(0.0, seconds=0.4)
    tween.set(100.0)
    start = tween._last
    values = [tween.step(start + 0.05 * i) for i in range(1, 60)]
    assert 0 < values[0] < 50 and values == sorted(values) and values[-1] == 100.0 and not tween.moving


def test_animations_off_means_final_state_at_once() -> None:
    from app.ui import motion

    motion.set_enabled(False)
    try:
        tween = motion.Tween(0.0)
        tween.set(80.0)
        assert tween.value == 80.0 and not tween.moving
    finally:
        motion.set_enabled(True)


def test_ease_out() -> None:
    from app.ui.motion import ease_out

    assert ease_out(0) == 0 and ease_out(1) == 1 and ease_out(0.5) > 0.5 and ease_out(2) == 1


def test_animations_setting_by_voice() -> None:
    from app.tools.voice_control import FRIENDLY_SETTINGS

    assert FRIENDLY_SETTINGS["animations"][0] == "animations" and Settings().animations is True
