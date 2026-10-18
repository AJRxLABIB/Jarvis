"""The daily plan: what to do today, in what order, and why - from the to-dos, estimates and calendar you really have.

It reuses the existing to-do list (``todos``) and the calendar callable; it creates no tasks of its own. A saved plan is
only a list of to-do ids with reasons (``day_plans``), so nothing is duplicated and the later review compares the plan
with what is actually marked done.

How an item is scored (all visible in its ``reason``): overdue (more days = more urgent), due today, priority, and a nudge
for items postponed several times (so they stop silently sliding). Items waiting for another open item are never planned
- they are listed as blocked. An item without an estimate is planned at 30 minutes and marked "estimate assumed". The
plan fills the available time; what doesn't fit is listed with why. Overdue and high-priority items that don't fit are
flagged as over budget instead of being hidden. Nothing is deleted, sent or changed on a calendar.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

DEFAULT_MINUTES = 30
WORK_END_HOUR = 18


@dataclass
class PlanItem:
    id: int
    text: str
    minutes: int
    assumed: bool
    reason: str
    over_budget: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Plan:
    day: str
    items: list[PlanItem] = field(default_factory=list)
    not_today: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    available: int = 0
    planned: int = 0

    @property
    def next_action(self) -> PlanItem | None:
        return self.items[0] if self.items else None

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "available_minutes": self.available, "planned_minutes": self.planned,
                "free_minutes": max(self.available - self.planned, 0),
                "items": [i.to_dict() for i in self.items], "not_today": self.not_today, "blocked": self.blocked,
                "conflicts": self.conflicts, "warnings": self.warnings,
                "next_action": self.next_action.to_dict() if self.next_action else None}


def _parse(value: Any, ref: datetime | None = None) -> datetime | None:
    """ISO text, or the calendar tool's 'Wed 14 Oct 15:00' (no year: the year of ``ref``, i.e. today)."""
    if isinstance(value, datetime):
        return value
    for fmt in (None, "%Y-%m-%d %H:%M", "%a %d %b %H:%M"):
        try:
            if fmt is None:
                return datetime.fromisoformat(str(value))
            parsed = datetime.strptime(str(value), fmt)
            return parsed.replace(year=(ref or datetime.now()).year) if fmt.startswith("%a") else parsed
        except ValueError:
            continue
    return None


def busy_intervals(events: list[dict], ref: datetime | None = None) -> list[tuple[datetime, datetime, str]]:
    out = []
    for e in events or []:
        start, end = _parse(e.get("start"), ref), _parse(e.get("end"), ref)
        if start and end and end > start:
            out.append((start.replace(tzinfo=None), end.replace(tzinfo=None), str(e.get("subject", ""))[:60]))
    return sorted(out)


def available_minutes(now: datetime, events: list[dict] | None = None, interruptions: int = 0,
                      work_end_hour: int = WORK_END_HOUR) -> int:
    """Minutes left in the working day, minus calendar time that overlaps it (counted once) and any interruption."""
    now = now.replace(tzinfo=None)
    end = now.replace(hour=work_end_hour, minute=0, second=0, microsecond=0)
    if end <= now:
        return 0
    busy = 0.0
    cursor = now
    for start, stop, _ in busy_intervals(events or [], now):
        start, stop = max(start, cursor), min(stop, end)
        if stop > start:
            busy += (stop - start).total_seconds() / 60
            cursor = stop
    return max(int((end - now).total_seconds() / 60 - busy) - max(interruptions, 0), 0)


def conflicts(events: list[dict] | None, ref: datetime | None = None) -> list[str]:
    spans = busy_intervals(events or [], ref)
    found = []
    for i, (s1, e1, n1) in enumerate(spans):
        for s2, e2, n2 in spans[i + 1:]:
            if s2 < e1:
                found.append(f"'{n1}' ({s1:%H:%M}-{e1:%H:%M}) overlaps '{n2}' ({s2:%H:%M}-{e2:%H:%M})")
    return found


def score(item: dict, today: date) -> tuple[int, str]:
    points, why = 0, []
    due = item.get("due")
    if due:
        days = (today - date.fromisoformat(due)).days
        if days > 0:
            points += 100 + min(days, 30)
            why.append(f"overdue by {days} day{'s' if days != 1 else ''}")
        elif days == 0:
            points += 60
            why.append("due today")
        else:
            why.append(f"due in {-days} day{'s' if days != -1 else ''}")
            points += max(20 + days * 2, 0)
    priority = item.get("priority", 2)
    points += {1: 30, 2: 15, 3: 0}.get(priority, 15)
    if priority == 1:
        why.append("high priority")
    if item.get("postponed", 0) >= 3:
        points += 10
        why.append(f"postponed {item['postponed']} times")
    return points, ", ".join(why) or "no deadline"


def build(items: list[dict], now: datetime, available: int, events: list[dict] | None = None) -> Plan:
    today = now.date()
    plan = Plan(today.isoformat(), available=available, conflicts=conflicts(events, now))
    ranked = []
    for item in items:
        if item.get("blocked_by"):
            plan.blocked.append({"id": item["id"], "item": item["text"], "waiting_for": item["blocked_by"]})
            continue
        points, why = score(item, today)
        ranked.append((points, item, why))
        if item.get("postponed", 0) >= 3:
            plan.warnings.append(f"'{item['text']}' has been postponed {item['postponed']} times: do it, split it, or drop it.")
    ranked.sort(key=lambda r: (-r[0], r[1].get("due") or "9999", r[1]["id"]))
    left = available
    for points, item, why in ranked:
        minutes = int(item.get("minutes") or DEFAULT_MINUTES)
        assumed = not item.get("minutes")
        urgent = points >= 100 or item.get("priority") == 1
        entry = PlanItem(item["id"], item["text"], minutes, assumed,
                         why + (", estimate assumed" if assumed else ""))
        if minutes <= left:
            plan.items.append(entry)
            left -= minutes
        elif urgent:
            entry.over_budget = True
            entry.reason += ", does not fit the time left today but is too important to hide"
            plan.items.append(entry)
            left = 0
        else:
            plan.not_today.append({"id": item["id"], "item": item["text"], "minutes": minutes,
                                   "why": f"needs {minutes} min and only {max(left, 0)} are left"})
    plan.planned = sum(i.minutes for i in plan.items)
    if plan.available and plan.planned > plan.available:
        plan.warnings.append(f"The plan is {plan.planned - plan.available} minutes over the time you have.")
    if not plan.available:
        plan.warnings.append("There is no working time left today; everything is listed as not today unless urgent.")
    return plan


def describe(plan: Plan) -> str:
    if not plan.items and not plan.blocked and not plan.not_today:
        return "Nothing is on your list, so there is nothing to plan. Add a to-do (with minutes if you can)."
    lines = [f"Plan for {plan.day}: {plan.planned} of {plan.available} available minutes."]
    if plan.next_action:
        n = plan.next_action
        lines.append(f"Next: {n.text} ({n.minutes} min) - {n.reason}.")
    for number, i in enumerate(plan.items, 1):
        lines.append(f"{number}. {i.text} - {i.minutes} min ({i.reason})" + (" [OVER BUDGET]" if i.over_budget else ""))
    for n in plan.not_today[:5]:
        lines.append(f"Not today: {n['item']} - {n['why']}.")
    for b in plan.blocked[:5]:
        lines.append(f"Blocked: {b['item']} waits for '{b['waiting_for']}'.")
    lines += [f"Conflict: {c}." for c in plan.conflicts] + [f"Note: {w}" for w in plan.warnings]
    return "\n".join(lines)


# ------------------------------------------------------------------ saved plan and review


def save(database, plan: Plan, now: datetime) -> None:
    payload = json.dumps({"ids": [i.id for i in plan.items], "available": plan.available, "made_at": now.isoformat()})
    with database.transaction() as conn:
        conn.execute("INSERT INTO day_plans (day, plan, updated_at) VALUES (?, ?, ?) ON CONFLICT(day) DO UPDATE SET "
                     "plan = excluded.plan, updated_at = excluded.updated_at", (plan.day, payload, now.isoformat()))


def saved_ids(database, day: str) -> list[int] | None:
    with database.connect() as conn:
        row = conn.execute("SELECT plan FROM day_plans WHERE day = ?", (day,)).fetchone()
    return json.loads(row[0])["ids"] if row else None


def review(database, now: datetime) -> dict[str, Any]:
    """The saved plan against what is really marked done today (read from the to-do list, never re-created)."""
    day = now.date().isoformat()
    ids = saved_ids(database, day)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with database.connect() as conn:
        done = {r["id"]: r["text"] for r in conn.execute("SELECT id, text FROM todos WHERE done = 1 AND done_at >= ?",
                                                         (midnight,))}
        open_ = {r["id"]: r["text"] for r in conn.execute("SELECT id, text FROM todos WHERE done = 0")}
    if ids is None:
        return {"planned": False, "done_today": list(done.values())}
    planned_done = [done[i] for i in ids if i in done]
    still_open = [open_[i] for i in ids if i in open_]
    gone = [i for i in ids if i not in done and i not in open_]  # deleted or replaced by a repeating copy
    unplanned = [t for i, t in done.items() if i not in ids]
    return {"planned": True, "planned_count": len(ids), "completed": planned_done, "not_done": still_open,
            "unplanned_done": unplanned, "no_longer_on_list": len(gone)}


def describe_review(r: dict[str, Any]) -> str:
    if not r["planned"]:
        return ("No plan was made today. " + (f"You finished: {', '.join(r['done_today'])}." if r["done_today"]
                                              else "Nothing is marked done yet."))
    text = f"Of {r['planned_count']} planned item(s), {len(r['completed'])} done"
    text += f"; not done: {', '.join(r['not_done'])}" if r["not_done"] else "; none left over"
    if r["unplanned_done"]:
        text += f". Also finished (not planned): {', '.join(r['unplanned_done'])}"
    if r["no_longer_on_list"]:
        text += f". {r['no_longer_on_list']} planned item(s) are no longer on the list"
    return text + "."
