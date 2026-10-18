from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config.settings import Settings
from app.core.voice_stats import VoiceStats
from app.memory.database import Database
from app.tools.base import ToolContext
from app.tools.diagnostics_tools import VoiceStatsArgs, voice_stats


def stats(tmp_path: Path) -> VoiceStats:
    db = Database(tmp_path / "v.db")
    db.initialize()
    return VoiceStats(db)


def test_empty_history_says_so(tmp_path: Path) -> None:
    s = stats(tmp_path)
    assert s.report()["heard"] == 0 and "Nothing has been heard" in s.describe()


def test_report_by_language(tmp_path: Path) -> None:
    s = stats(tmp_path)
    a = s.record("ok", language="en", confidence=0.9, stt_ms=400, handled=True)
    s.first_reply(a, 900)
    b = s.record("ok", language="en", confidence=0.8, stt_ms=600, handled=False)
    s.first_reply(b, 1100)
    s.record("ok", language="bn", confidence=0.6, stt_ms=1500)
    s.record("low_confidence", language="bn", confidence=0.2, stt_ms=1400)
    s.record("wake_only")
    report = s.report()
    en = report["by_language"]["en"]
    assert en["commands"] == 2 and en["avg_recognition_ms"] == 500 and en["avg_first_reply_ms"] == 1000
    assert en["handled_rate"] == 50
    assert report["by_language"]["bn"]["low_confidence"] == 1
    assert report["wakes_without_command"] == 1 and report["low_confidence"] == 1
    text = s.describe()
    assert "not proof" in text and "recognition 500 ms" in text


def test_old_rows_are_dropped_and_no_words_are_stored(tmp_path: Path) -> None:
    s = stats(tmp_path)
    s.record("ok", language="en", now=datetime.now(timezone.utc) - timedelta(days=90))
    s.record("ok", language="en")
    assert s.report(days=60)["heard"] == 1
    with s._db.connect() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(voice_events)")}
    assert not columns & {"text", "audio", "transcript"}


def test_the_tool(tmp_path: Path) -> None:
    s = stats(tmp_path)
    s.record("ok", language="en", stt_ms=300)
    result = voice_stats(VoiceStatsArgs(days=7), ToolContext(Settings(), database=s._db))
    assert result.ok and result.data["heard"] == 1
