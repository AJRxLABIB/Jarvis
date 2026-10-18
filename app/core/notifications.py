"""The notification center: everything Jarvis wanted to tell you, in one list you can go through.

Each notification: when, from what (source), a category (reminder, alert, mission, security, system,
suggestion), a priority (low / normal / high / urgent), a title and text, read / archived, and optionally an
action - a command Jarvis itself wrote (e.g. "clean temp files"), which you can run with one click. The text of a
notification from outside content (an email subject, say) is data; actions only ever come from Jarvis's own
rules below, never from that text.

Notifications are created from events (app/core/events.py) by RULES; the mode decides which are also spoken.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core import events

if TYPE_CHECKING:
    from app.memory.database import Database

PRIORITIES = ("low", "normal", "high", "urgent")
CATEGORIES = ("reminder", "alert", "mission", "security", "system", "suggestion", "phone")


@dataclass(frozen=True)
class Notice:
    id: int
    ts: str
    source: str
    category: str
    priority: str
    title: str
    body: str
    action: str
    read: bool
    archived: bool


class NotificationCenter:
    KEEP = 500
    DEDUPE_MINUTES = 30       # the same unread notice again within this time is not added twice
    RETENTION_DAYS = 90       # read notices older than this are removed (unread ones stay until you see them)

    def __init__(self, database: "Database") -> None:
        self._db = database
        self.duplicate = False    # the last add() found the same unread notice already there

    def add(self, title: str, body: str = "", *, source: str, category: str, priority: str = "normal",
            action: str = "") -> int:
        from datetime import datetime, timedelta, timezone

        from app.memory.database import utc_now_iso

        if category not in CATEGORIES or priority not in PRIORITIES:
            raise ValueError("bad category or priority")
        now = datetime.now(timezone.utc)
        since = (now - timedelta(minutes=self.DEDUPE_MINUTES)).isoformat(timespec="seconds")
        with self._db.transaction() as conn:
            same = conn.execute("SELECT id FROM notifications WHERE read = 0 AND archived = 0 AND category = ? AND "
                                "title = ? AND body = ? AND ts >= ? ORDER BY id DESC LIMIT 1",
                                (category, title[:200], body[:1000], since)).fetchone()
            self.duplicate = same is not None
            if same is not None:
                return int(same[0])
            conn.execute("DELETE FROM notifications WHERE read = 1 AND ts < ?",
                         ((now - timedelta(days=self.RETENTION_DAYS)).isoformat(timespec="seconds"),))
            new_id = conn.execute("INSERT INTO notifications (ts, source, category, priority, title, body, action) "
                                  "VALUES (?, ?, ?, ?, ?, ?, ?)", (utc_now_iso(), source[:40], category, priority,
                                                                    title[:200], body[:1000], action[:200])).lastrowid
            conn.execute("DELETE FROM notifications WHERE id <= (SELECT MAX(id) FROM notifications) - ?", (self.KEEP,))
        return int(new_id)

    def items(self, *, unread_only: bool = False, category: str | None = None, include_archived: bool = False,
              limit: int = 100) -> list[Notice]:
        where, params = ["1 = 1"], []
        if not include_archived:
            where.append("archived = 0")
        if unread_only:
            where.append("read = 0")
        if category:
            where.append("category = ?")
            params.append(category)
        with self._db.connect() as conn:
            rows = conn.execute(f"SELECT * FROM notifications WHERE {' AND '.join(where)} "
                                "ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, id DESC "
                                "LIMIT ?", (*params, limit)).fetchall()
        return [Notice(r["id"], r["ts"], r["source"], r["category"], r["priority"], r["title"], r["body"], r["action"],
                       bool(r["read"]), bool(r["archived"])) for r in rows]

    def unread_count(self) -> int:
        with self._db.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM notifications WHERE read = 0 AND archived = 0").fetchone()[0])

    def mark_read(self, ids: list[int] | None = None) -> int:
        with self._db.transaction() as conn:
            if ids is None:
                return conn.execute("UPDATE notifications SET read = 1 WHERE read = 0").rowcount
            return sum(conn.execute("UPDATE notifications SET read = 1 WHERE id = ?", (i,)).rowcount for i in ids)

    def archive(self, ids: list[int] | None = None) -> int:
        with self._db.transaction() as conn:
            if ids is None:
                return conn.execute("UPDATE notifications SET archived = 1, read = 1 WHERE archived = 0").rowcount
            return sum(conn.execute("UPDATE notifications SET archived = 1, read = 1 WHERE id = ?", (i,)).rowcount
                       for i in ids)


# ------------------------------------------------------------------ rules: event -> notification

def rules(event: events.Event) -> tuple[str, str, str, str, str] | None:
    """(title, body, category, priority, action) for an event, or None (not everything needs telling)."""
    data = event.data
    name = event.name
    if name == events.MISSION_COMPLETED:
        return ("Mission completed", str(data.get("title", "")), "mission", "normal", "")
    if name == events.MISSION_PAUSED:
        return ("Mission paused", str(data.get("note", ""))[:300], "mission", "high", "continue my mission")
    if name == events.BATTERY_LOW:
        return ("Battery low", str(data.get("message", "")), "alert", "urgent" if data.get("critical") else "high", "")
    if name == events.PC_PROBLEM:
        return ("PC watcher", str(data.get("message", "")), "alert", "normal", str(data.get("fix", "")))
    if name == events.DOWNLOAD_COMPLETED:
        return ("Download finished", str(data.get("message", "")), "system", "low", "")
    if name == events.REMINDER:
        return (str(data.get("title", "Reminder")), str(data.get("message", "")), "reminder", "high", "")
    if name == events.SUGGESTION:
        return ("Suggestion", str(data.get("message", "")), "suggestion", "low", str(data.get("action", "")))
    if name == events.SECURITY_EVENT:
        return ("Security", str(data.get("message", "")), "security", str(data.get("priority", "high")), "")
    if name == events.EMERGENCY:
        return ("Emergency controls", str(data.get("message", "")), "security", "urgent", "")
    if name == events.JARVIS_UPDATED:
        return (f"Jarvis updated to {data.get('version', '')}", str(data.get("previous", "")), "system", "low", "")
    return None


def connect(bus: events.EventBus, center: NotificationCenter, on_new=None) -> None:
    """Make notifications from events; ``on_new(id, priority, title, body)`` tells the window."""
    def handle(event: events.Event) -> None:
        made = rules(event)
        if made is None:
            return
        title, body, category, priority, action = made
        new_id = center.add(title, body, source=event.name, category=category, priority=priority, action=action)
        if on_new is not None and not center.duplicate:     # the same unread notice again: not shown twice
            on_new(new_id, priority, title, body)

    bus.subscribe("*", handle)
