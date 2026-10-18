from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config.settings import Settings
from app.core import metrics
from app.memory.database import Database
from app.tools.base import ToolContext
from app.tools.diagnostics_tools import HealthHistoryArgs, health_history

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def rows(count: int, cpu=20.0, mem=50.0, end_cpu=None):
    out = []
    for i in range(count):
        value = end_cpu if end_cpu is not None and i >= count - 15 else cpu + (i % 5)
        out.append({"ts": (NOW - timedelta(minutes=count - i)).isoformat(), "cpu": value, "mem_percent": mem,
                    "disk_percent": 40.0, "battery": None, "plugged": None})
    return out


def test_baseline_needs_enough_history() -> None:
    assert metrics.baseline(rows(50)) == {}
    base = metrics.baseline(rows(400))
    assert base["cpu"]["p90"] >= base["cpu"]["median"] and base["cpu"]["samples"] == 400


def test_above_normal_is_an_inference_with_confidence() -> None:
    quiet = metrics.analyse_baseline(rows(400))
    assert quiet == []
    loud = metrics.analyse_baseline(rows(400, end_cpu=85.0))
    assert len(loud) == 1 and loud[0].basis == "inferred" and loud[0].confidence == "medium"
    assert "inferred, medium confidence" in loud[0].line() and "usually at most" in loud[0].evidence
    assert metrics.analyse_baseline(rows(400, end_cpu=45.0)) == []  # under 50 % is never called unusual


def test_measured_findings_say_measured() -> None:
    finding = metrics.Finding("x", "info", "m", "e", "s")
    assert "measured" in finding.line()


def test_delete_and_tool(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.db")
    db.initialize()
    store = metrics.MetricsStore(db)
    now = datetime.now(timezone.utc)
    with db.transaction() as conn:
        for i in range(3):
            conn.execute("INSERT INTO metrics (ts, cpu, mem_percent) VALUES (?, ?, ?)",
                         ((now - timedelta(days=10 * i, minutes=5)).isoformat(timespec="seconds"), 10 + i, 40))
    ctx = ToolContext(Settings(), database=db)
    assert health_history(HealthHistoryArgs(action="delete", older_than_days=5), ctx).data["removed"] == 2
    assert "Not enough history" in health_history(HealthHistoryArgs(action="baseline"), ctx).message
    assert health_history(HealthHistoryArgs(action="delete"), ctx).data["removed"] == 1
    assert store.history(24 * 365) == []


def test_retention_is_configurable(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.db")
    db.initialize()
    short = metrics.MetricsStore(db, timedelta(days=2))
    short.record({"cpu": 1}, now=NOW - timedelta(days=5))
    short.record({"cpu": 2}, now=NOW)  # this write drops the 5-day-old row
    assert [r["cpu"] for r in short.history(24 * 30, NOW + timedelta(days=1))] == [2]
