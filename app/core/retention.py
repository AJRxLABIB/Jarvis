"""Retention controls: ONE place that shows what Jarvis keeps, for how long, how much of it there is, and clears one store on request.

Nothing new is stored here: it reads the existing stores (notification center, event log, clipboard memory, health readings, what Jarvis recorded about
your activity, finished cross-device tasks, closed guardian incidents) and their existing retention settings. ``clear`` removes ONE store's data and says
honestly what that does not cover: backups, log files and exported files are separate copies and are not touched here.
Pure over a database + settings store, tested with a temporary database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCOPE_NOTE = "Backups, log files and anything you exported are separate copies and are not removed by this."


@dataclass
class Row:
    key: str
    label: str
    count: int | None
    keeps: str
    setting: str                 # the setting that controls it ("" = fixed)
    clearable: bool = True

    def line(self) -> str:
        n = "unknown" if self.count is None else str(self.count)
        change = f" (change: {self.setting})" if self.setting else ""
        return f"{self.label}: {n} item(s); keeps {self.keeps}{change}"


def _count(db: Any, sql: str) -> int | None:
    try:
        with db.connect() as conn:
            return int(conn.execute(sql).fetchone()[0])
    except Exception:
        return None


def status(db: Any, settings: Any, kv: Any) -> list[Row]:
    days = lambda name, default: int(getattr(settings, name, default) or 0)          # noqa: E731
    mem_days = days("memory_event_retention_days", 0)
    inc = [i for i in (kv.get("guardian_incidents", []) or []) if i.get("status") != "open"]
    tx = [t for t in (kv.get("transactions", []) or []) if t.get("state") in ("complete", "stopped")]
    return [
        Row("notifications", "Notification center", _count(db, "SELECT COUNT(*) FROM notifications"), "unread until you see them; read ones 90 days (max 500)", ""),
        Row("events", "Event log", _count(db, "SELECT COUNT(*) FROM events"), "the newest 2000 events", ""),
        Row("clipboard", "Clipboard memory", _count(db, "SELECT COUNT(*) FROM clipboard_items"), f"{days('clipboard_retention_days', 7)} days (pinned stay)", "CLIPBOARD_RETENTION_DAYS"),
        Row("metrics", "Health readings", _count(db, "SELECT COUNT(*) FROM metrics"), f"{days('metrics_retention_days', 14)} days", "METRICS_RETENTION_DAYS"),
        Row("activity", "What Jarvis recorded about your activity", _count(db, "SELECT COUNT(*) FROM memories WHERE provenance = 'event'"),
            f"{mem_days} days" if mem_days else "until you delete it (no limit set)", "MEMORY_EVENT_RETENTION_DAYS"),
        Row("transactions", "Finished cross-device tasks", len(tx), "the 30 newest tasks", ""),
        Row("incidents", "Closed guardian incidents", len(inc), "the 100 newest incidents", ""),
    ]


def describe(rows: list[Row]) -> str:
    return "\n".join(r.line() for r in rows) + "\n" + SCOPE_NOTE


def clear(key: str, db: Any, settings: Any, kv: Any) -> tuple[int, str]:
    """Remove everything in ONE store. Returns (how many, honest note). Unknown key -> ValueError."""
    if key == "notifications":
        with db.transaction() as conn:
            n = conn.execute("DELETE FROM notifications WHERE read = 1 OR archived = 1").rowcount
        return n, "Read and archived notifications removed; unread ones were kept."
    if key == "events":
        with db.transaction() as conn:
            n = conn.execute("DELETE FROM events").rowcount
        return n, "The event log was emptied (notifications already created from it stay)."
    if key == "clipboard":
        from app.core.clipboard_memory import ClipboardStore

        return ClipboardStore(db, int(getattr(settings, "clipboard_retention_days", 7) or 7)).clear(), "Clipboard memory cleared (pinned items too)."
    if key == "metrics":
        from datetime import timedelta

        from app.core import metrics

        return metrics.MetricsStore(db, timedelta(days=14)).delete(), "Health readings removed."
    if key == "activity":
        with db.transaction() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM memories WHERE provenance = 'event'")]
        from app.memory.manager import MemoryManager

        mm = MemoryManager(db)
        n = 0
        for mid in ids:
            try:
                mm.forget(mid)
                n += 1
            except Exception:
                continue
        return n, "What Jarvis recorded about your activity was removed. Memories you saved yourself were not touched."
    if key == "transactions":
        items = kv.get("transactions", []) or []
        keep = [t for t in items if t.get("state") not in ("complete", "stopped")]
        kv.set("transactions", keep)
        return len(items) - len(keep), "Finished cross-device tasks removed; open ones were kept."
    if key == "incidents":
        items = kv.get("guardian_incidents", []) or []
        keep = [i for i in items if i.get("status") == "open"]
        kv.set("guardian_incidents", keep)
        return len(items) - len(keep), "Closed guardian incidents removed; open ones were kept."
    raise ValueError(f"Unknown store '{key}'.")


KEYS = ("notifications", "events", "clipboard", "metrics", "activity", "transactions", "incidents")
