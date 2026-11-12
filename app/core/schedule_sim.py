"""Schedule what-if: "Can I finish these this week?", "What if I postpone X?", "What if I study 2 h a day?" - read-only simulation.

It copies the open to-dos into a plan, schedules them earliest-deadline-first into the working hours you have per day, and reports which
finish late. A scenario (postpone, extra daily block, new task, different hours) is applied to the COPY and compared with the baseline.
Nothing in the database is changed; applying a plan is a separate, confirmed action.

Honest limits (always printed with the answer):
* effort per task comes from the task text ("(2h)", "[90m]"), from ``estimates`` you give, or a flat default that is flagged as a guess;
* calendar events are only counted when passed in (``busy``); no calendar connected = free time is an assumption;
* a plan that fits on paper is not a promise: breaks, interruptions and bad estimates are not modelled, so confidence is capped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

DEFAULT_MIN = 60
MAX_DAYS = 60
_EST = re.compile(r"[\(\[]\s*~?(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?)\s*[\)\]]", re.I)


@dataclass
class Task:
    key: str
    text: str
    minutes: int
    guessed: bool
    due: date | None
    source: str = "todo"


@dataclass
class Outcome:
    key: str
    text: str
    finish: date | None
    due: date | None
    late_days: int          # > 0 late, 0 on the day, < 0 slack days; 0 with no due
    unscheduled: bool = False


@dataclass
class Result:
    outcomes: list[Outcome]
    free_days: int
    used_min: int
    capacity_min: int


@dataclass
class Comparison:
    baseline: Result
    scenario: Result
    changes: list[str]
    assumptions: list[str]
    confidence: str
    notes: list[str] = field(default_factory=list)


def estimate_from_text(text: str) -> int | None:
    m = _EST.search(text or "")
    if not m:
        return None
    v = float(m.group(1))
    return max(5, round(v * 60)) if m.group(2).lower().startswith("h") else max(5, round(v))


def _day(v: str | None) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def tasks_from(todos: list[dict], estimates: dict[str, float] | None = None, default_min: int = DEFAULT_MIN) -> list[Task]:
    est = {str(k): v for k, v in (estimates or {}).items()}
    out = []
    for t in todos:
        key = str(t.get("id"))
        given = est.get(key)
        mins = round(given * 60) if given is not None else estimate_from_text(t.get("text", ""))
        out.append(Task(key, str(t.get("text", ""))[:80], int(mins) if mins else default_min, mins is None, _day(t.get("due"))))
    return out


def schedule(tasks: list[Task], today: date, hours_per_day: float, busy: dict[date, int] | None = None,
             skip_weekdays: tuple[int, ...] = (), extra_daily_min: int = 0, horizon: int = 30) -> Result:
    """Earliest deadline first, then smallest first. ``extra_daily_min`` is a protected block taken out of each day's capacity."""
    horizon = max(1, min(horizon, MAX_DAYS))
    cap = max(0, round(hours_per_day * 60) - extra_daily_min)
    left = {}
    days = []
    for i in range(horizon):
        d = today + timedelta(days=i)
        if d.weekday() in skip_weekdays:
            continue
        left[d] = max(0, cap - (busy or {}).get(d, 0))
        days.append(d)
    order = sorted(tasks, key=lambda t: (t.due is None, t.due or date.max, t.minutes))
    outcomes, used = [], 0
    for t in order:
        need, finish = t.minutes, None
        for d in days:
            if need <= 0:
                break
            take = min(need, left[d])
            if take:
                left[d] -= take
                need -= take
                used += take
                finish = d
        if need > 0:
            outcomes.append(Outcome(t.key, t.text, None, t.due, 9999, True))
        else:
            late = (finish - t.due).days if (t.due and finish) else 0
            outcomes.append(Outcome(t.key, t.text, finish, t.due, late))
    return Result(outcomes, sum(1 for d in days if left[d] == cap and cap), used, sum(max(0, cap - (busy or {}).get(d, 0)) for d in days))


def simulate(todos: list[dict], today: date, hours_per_day: float = 6.0, estimates: dict[str, float] | None = None,
             postpone: dict[str, int] | None = None, add: list[dict] | None = None, drop: list[str] | None = None,
             extra_daily_hours: float = 0.0, extra_label: str = "", scenario_hours_per_day: float | None = None,
             busy: dict[date, int] | None = None, horizon: int = 30, skip_weekdays: tuple[int, ...] = ()) -> Comparison:
    base_tasks = tasks_from(todos, estimates)
    base = schedule(base_tasks, today, hours_per_day, busy, skip_weekdays, horizon=horizon)
    tasks = [Task(**t.__dict__) for t in base_tasks]
    changes: list[str] = []
    for k, days in (postpone or {}).items():
        for t in tasks:
            if t.key == str(k):
                if t.due:
                    t.due += timedelta(days=int(days))
                    changes.append(f"postpone '{t.text}' by {days} day(s) -> due {t.due}")
                else:
                    changes.append(f"'{t.text}' has no due date, so postponing changes nothing")
    for k in drop or []:
        gone = [t for t in tasks if t.key == str(k)]
        tasks = [t for t in tasks if t.key != str(k)]
        changes += [f"drop '{t.text}'" for t in gone]
    for i, a in enumerate(add or []):
        mins = round(float(a.get("hours", 1)) * 60)
        tasks.append(Task(f"new{i + 1}", str(a.get("text", "new task"))[:80], max(5, mins), False, _day(a.get("due")), "scenario"))
        changes.append(f"add '{a.get('text', 'new task')}' ({mins} min" + (f", due {a['due']}" if a.get("due") else "") + ")")
    extra = round(max(0.0, extra_daily_hours) * 60)
    if extra:
        changes.append(f"protect {extra_daily_hours:g} h/day" + (f" for {extra_label}" if extra_label else "") + " (less time for the tasks)")
    hpd = scenario_hours_per_day if scenario_hours_per_day is not None else hours_per_day
    if scenario_hours_per_day is not None and scenario_hours_per_day != hours_per_day:
        changes.append(f"working hours {hours_per_day:g} -> {scenario_hours_per_day:g} h/day")
    sc = schedule(tasks, today, hpd, busy, skip_weekdays, extra_daily_min=extra, horizon=horizon)

    guessed = sum(1 for t in base_tasks if t.guessed)
    assumptions = [f"{hours_per_day:g} productive hours per day, " + ("weekdays only" if skip_weekdays else "every day (weekends included unless you say otherwise)"),
                   f"{guessed} of {len(base_tasks)} tasks have no effort estimate and use a flat {DEFAULT_MIN} min guess" if guessed else "every task has an effort estimate",
                   "calendar events counted" if busy else "no calendar events counted - free time is an assumption",
                   "no breaks, interruptions or estimate overruns are modelled"]
    share = guessed / len(base_tasks) if base_tasks else 0
    confidence = "low" if (share > 0.5 or not base_tasks) else "medium"  # never "high": a plan on paper is not a promise
    notes = []
    if not base_tasks:
        notes.append("There are no open to-dos to simulate.")
    return Comparison(base, sc, changes, assumptions, confidence, notes)


def describe(c: Comparison) -> str:
    def line(o: Outcome) -> str:
        if o.unscheduled:
            return f"  - {o.text}: does NOT fit in the horizon"
        if o.due is None:
            return f"  - {o.text}: done {o.finish} (no due date)"
        tag = f"{o.late_days} day(s) LATE" if o.late_days > 0 else ("on the due day" if o.late_days == 0 else f"{-o.late_days} day(s) early")
        return f"  - {o.text}: done {o.finish}, due {o.due} -> {tag}"

    def late(r: Result) -> int:
        return sum(1 for o in r.outcomes if o.unscheduled or o.late_days > 0)

    bl, sc = c.baseline, c.scenario
    out = [f"BASELINE: {late(bl)} of {len(bl.outcomes)} tasks miss their date.", *[line(o) for o in sorted(bl.outcomes, key=lambda o: (o.finish or date.max))[:12]]]
    if c.changes:
        out += ["", "SCENARIO (" + "; ".join(c.changes) + f"): {late(sc)} of {len(sc.outcomes)} tasks miss their date.", *[line(o) for o in sorted(sc.outcomes, key=lambda o: (o.finish or date.max))[:12]]]
        diff = late(sc) - late(bl)
        out.append(("Net effect: " + (f"{diff} more task(s) become late." if diff > 0 else f"{-diff} fewer task(s) are late." if diff < 0 else "no change in how many are late.")))
    out += ["", f"Confidence: {c.confidence} (a simulation, not a prediction).", "Assumptions: " + "; ".join(c.assumptions) + ".", *c.notes,
            "Nothing was changed - this only simulates."]
    return "\n".join(out)
