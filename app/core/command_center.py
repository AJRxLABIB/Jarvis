"""Command center: one honest snapshot of your day, built only from what is really stored.

Sections: to-dos (overdue / due today / blocked), active missions and automations (the task center), what finished
today (to-dos done, missions completed, requests handled and failed) and what is waiting for you (approvals,
paused missions). Nothing is invented: a source that can't be read is listed under ``unavailable`` instead of
being guessed, and there is no calendar section unless a calendar integration is configured.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.core import task_center


def _midnight_utc_iso(now: datetime) -> str:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat(timespec="seconds")  # a naive time is taken as local


def snapshot(ctx: Any, calendar: Callable[[], list[dict]] | None = None) -> dict[str, Any]:
    """``calendar``: returns today's events (Outlook), or raises when no calendar can be reached."""
    from app.tools import productivity

    now = ctx.clock()
    today = now.date().isoformat()
    data: dict[str, Any] = {"date": today, "unavailable": []}
    database = ctx.database

    try:
        items = productivity.open_todos(ctx)
        data["todos"] = {
            "open": len(items),
            "overdue": [i["text"] for i in items if i["due"] and i["due"] < today],
            "due_today": [i["text"] for i in items if i["due"] == today],
            "blocked": [{"item": i["text"], "waiting_for": i["blocked_by"]} for i in items if i["blocked_by"]],
            "often_postponed": [i["text"] for i in items if i["postponed"] >= 3],
        }
    except Exception:
        data["unavailable"].append("to-do list")

    try:
        tasks = task_center.collect(database, include_finished=False, limit=10)
        data["active"] = [t.to_dict() for t in tasks]
        data["waiting_for_you"] = [t.title for t in tasks if t.status in ("WAITING_FOR_APPROVAL", "PAUSED")]
    except Exception:
        data["unavailable"].append("task center")

    try:
        since = _midnight_utc_iso(now)
        with database.connect() as conn:
            todos_done = conn.execute("SELECT text FROM todos WHERE done = 1 AND done_at >= ?",
                                      (now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),)).fetchall()
            missions = conn.execute("SELECT COUNT(DISTINCT task_id) FROM task_events WHERE new_state = 'completed' "
                                    "AND ts >= ?", (_local_midnight(now),)).fetchone()[0]
            runs = conn.execute("SELECT state, COUNT(*) FROM agent_runs WHERE started_at >= ? GROUP BY state",
                                (since,)).fetchall()
        data["today"] = {"todos_done": [r[0] for r in todos_done], "missions_completed": missions,
                         "requests": {r[0]: r[1] for r in runs}}
    except Exception:
        data["unavailable"].append("today's history")

    data["calendar"] = None  # filled only when a calendar really answered; never assumed
    data["calendar_note"] = "this view doesn't read your calendar; ask me 'how is my day' for appointments"
    if calendar is not None:
        try:
            data["calendar"] = [{"subject": e.get("subject", ""), "start": e.get("start", ""), "end": e.get("end", ""),
                                 "location": e.get("location", "")} for e in calendar()]
            data["calendar_note"] = ""
        except Exception as exc:
            data["calendar_note"] = f"your calendar couldn't be read ({str(exc)[:80] or 'Outlook not available'})"
    return data


def _local_midnight(now: datetime) -> str:
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def describe(data: dict[str, Any], *, evening: bool = False) -> str:
    parts: list[str] = []
    todos = data.get("todos")
    if todos is not None and todos["open"]:
        line = f"{todos['open']} open to-do(s)"
        if todos["overdue"]:
            line += f", {len(todos['overdue'])} overdue ({'; '.join(todos['overdue'][:3])})"
        if todos["due_today"]:
            line += f", due today: {'; '.join(todos['due_today'][:3])}"
        if todos["blocked"]:
            line += f", {len(todos['blocked'])} blocked"
        if todos["often_postponed"]:
            line += f". Postponed 3+ times: {'; '.join(todos['often_postponed'][:2])}"
        parts.append(line)
    active = data.get("active")
    if active:
        parts.append(f"{len(active)} active background task(s): " + "; ".join(
            f"{t['title']} [{t['status']}]" for t in active[:4]))
    if data.get("waiting_for_you"):
        parts.append("waiting for you: " + "; ".join(data["waiting_for_you"][:3]))
    today = data.get("today")
    if today is not None and (evening or today["todos_done"] or today["missions_completed"]):
        done = today["todos_done"]
        text = f"today you completed {len(done)} to-do(s)" + (f" ({'; '.join(done[:4])})" if done else "")
        if today["missions_completed"]:
            text += f" and {today['missions_completed']} mission(s)"
        failed = today["requests"].get("failed", 0)
        if failed:
            text += f"; {failed} request(s) failed"
        parts.append(text)
    if data["unavailable"]:
        parts.append("couldn't read: " + ", ".join(data["unavailable"]))
    events = data.get("calendar")
    if events:
        nxt = events[0]
        parts.insert(0, f"{len(events)} appointment(s) today, first: {nxt['subject']} at {nxt['start']}")
    elif events == [] and not data.get("calendar_note"):
        parts.insert(0, "no appointments today")
    note = f" Appointments aren't included: {data['calendar_note']}." if events is None and data.get("calendar_note") else ""
    if not parts:
        return "Nothing on your plate." + note
    return f"Your day ({data['date']}): " + "; ".join(parts) + "." + note

