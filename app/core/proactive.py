"""Proactive intelligence: noticing things worth telling you, ranking them, and deciding whether to speak up.

Signals (only from what Jarvis may already see): goals due or overdue, goals nobody touched for a week, a paused
mission, many uncommitted changes in the project you're working on (context engine), the same tool failing again
and again (audit log). Each becomes a *candidate* with urgency, importance, confidence and interruption cost; the
score decides against your proactive level:

    off        nothing (except urgent security / battery alerts, if EMERGENCY_EXCEPTIONS)
    minimal    only the most important (score >= 0.75)
    normal     important things (>= 0.55)             <- default
    active     useful things (>= 0.40)
    high       most things (>= 0.25)

Then: categories you switched off are dropped; the same thing isn't repeated for 6 hours; in quiet hours and
beyond the hourly limit a suggestion only goes to the notification center (not spoken); modes and focus apply as
usual. Proactive means *telling* you: a suggestion can carry a command Jarvis wrote ("continue my mission"), which
runs only when you choose it, through the normal checks. Nothing here executes anything.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("core.proactive")

LEVELS = {"off": 2.0, "minimal": 0.75, "normal": 0.55, "active": 0.40, "high": 0.25}
CATEGORIES = ("goal", "mission", "project", "system", "suggestion", "security", "reminder")
REPEAT_HOURS = 6
UNCOMMITTED_LIMIT = 10


@dataclass(frozen=True)
class Candidate:
    key: str  # the same key isn't told twice within REPEAT_HOURS
    category: str
    title: str
    message: str
    urgency: float  # 0..1 how soon it matters
    importance: float  # 0..1 how much it matters
    confidence: float = 1.0  # 0..1 how sure the signal is (1.0 = read directly)
    interruption: float = 0.3  # 0..1 how costly it is to interrupt for it
    action: str = ""  # a command Jarvis wrote; runs only if you choose it
    emergency: bool = False  # urgent security / battery: may pass even when proactive is off

    @property
    def score(self) -> float:
        value = 0.45 * self.urgency + 0.35 * self.importance + 0.2 * self.confidence - 0.25 * self.interruption
        return round(max(0.0, min(1.0, value + 0.1)), 3)


@dataclass(frozen=True)
class Decision:
    outcome: str  # speak | notify | drop
    reason: str
    candidate: Candidate
    at: datetime


def parse_quiet_hours(text: str) -> tuple[time, time] | None:
    """'22:00-07:00' -> (22:00, 07:00); '' -> None."""
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*", text or "")
    if not match:
        return None
    start = time(int(match.group(1)) % 24, int(match.group(2) or 0))
    end = time(int(match.group(3)) % 24, int(match.group(4) or 0))
    return start, end


def in_quiet_hours(now: datetime, quiet: tuple[time, time] | None) -> bool:
    if quiet is None:
        return False
    start, end, t = quiet[0], quiet[1], now.time()
    return (start <= t < end) if start <= end else (t >= start or t < end)


def reminder_may_speak(settings, now: datetime) -> bool:
    """A reminder is always shown and kept in the notification center, but it is only SPOKEN outside quiet hours."""
    return not in_quiet_hours(now, parse_quiet_hours(getattr(settings, "quiet_hours", "")))


@dataclass
class ProactiveEngine:
    delivered: dict[str, datetime] = field(default_factory=dict)
    history: list[Decision] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def decide(self, candidate: Candidate, settings, now: datetime) -> Decision:
        level = getattr(settings, "proactive_level", "normal")
        allowed = {c.strip() for c in (getattr(settings, "proactive_categories", "") or "").split(",") if c.strip()}
        quiet = parse_quiet_hours(getattr(settings, "quiet_hours", ""))
        limit = int(getattr(settings, "proactive_max_per_hour", 4))
        exceptions = bool(getattr(settings, "proactive_emergency_exceptions", True))
        with self._lock:
            last = self.delivered.get(candidate.key)
            if last is not None and now - last < timedelta(hours=REPEAT_HOURS):
                return self._record(Decision("drop", "already told you in the last 6 hours", candidate, now))
            emergency = candidate.emergency and exceptions
            if allowed and candidate.category not in allowed and not emergency:
                return self._record(Decision("drop", f"'{candidate.category}' is switched off", candidate, now))
            threshold = LEVELS.get(level, LEVELS["normal"])
            if candidate.score < threshold and not emergency:
                return self._record(Decision("drop", f"score {candidate.score} is below your '{level}' level "
                                             f"({threshold})", candidate, now))
            spoken_last_hour = sum(1 for d in self.history if d.outcome == "speak" and now - d.at < timedelta(hours=1))
            self.delivered[candidate.key] = now
            if emergency:
                return self._record(Decision("speak", "urgent: always told", candidate, now))
            if in_quiet_hours(now, quiet):
                return self._record(Decision("notify", "quiet hours: in the notification center only", candidate, now))
            if spoken_last_hour >= limit:
                return self._record(Decision("notify", f"{limit} suggestions already this hour: in the notification "
                                             "center only", candidate, now))
            return self._record(Decision("speak", f"score {candidate.score} >= '{level}' ({threshold})", candidate,
                                         now))

    def _record(self, decision: Decision) -> Decision:
        self.history = (self.history + [decision])[-100:]
        return decision

    def recent(self, limit: int = 10) -> list[Decision]:
        with self._lock:
            return list(self.history[-limit:])[::-1]

    def clear(self) -> None:
        with self._lock:
            self.delivered.clear()
            self.history.clear()


ENGINE = ProactiveEngine()


# ------------------------------------------------------------------ signals


def candidates(ctx, now: datetime) -> list[Candidate]:
    """Everything worth considering right now (cheap reads; no screen, no network)."""
    found: list[Candidate] = []
    if ctx.database is None:
        return found
    found += _goal_signals(ctx, now.date())
    found += _mission_signals(ctx, now)
    found += _project_signals(ctx)
    found += _failure_signals(ctx)
    return found


def _goal_signals(ctx, today: date) -> list[Candidate]:
    out = []
    try:
        with ctx.database.connect() as conn:
            goals = conn.execute("SELECT id, title, deadline, progress, updated_at, priority FROM goals "
                                 "WHERE status = 'active'").fetchall()
    except Exception:
        return out
    for goal in goals:
        importance = {1: 0.9, 2: 0.6, 3: 0.4}.get(goal["priority"], 0.6)
        if goal["deadline"]:
            left = (date.fromisoformat(goal["deadline"]) - today).days
            if left < 0:
                out.append(Candidate(f"goal-overdue:{goal['id']}", "goal", "Goal overdue",
                                     f"\"{goal['title']}\" was due {-left} day(s) ago and is at {goal['progress']}%. "
                                     "Shall we re-plan it?", 1.0, importance, action=f"show my goal {goal['title']}"))
            elif left <= 1 and goal["progress"] < 100:
                out.append(Candidate(f"goal-due:{goal['id']}:{today}", "goal", "Goal due soon",
                                     f"\"{goal['title']}\" is due {'today' if left == 0 else 'tomorrow'} and is at "
                                     f"{goal['progress']}%.", 0.9, importance))
        try:
            updated = datetime.fromisoformat(goal["updated_at"]).date()
        except (TypeError, ValueError):
            continue
        if (today - updated).days >= 7:
            out.append(Candidate(f"goal-stale:{goal['id']}", "goal", "Goal waiting",
                                 f"Nothing happened on \"{goal['title']}\" for {(today - updated).days} days.",
                                 0.3, importance, interruption=0.5))
    return out


def _mission_signals(ctx, now: datetime) -> list[Candidate]:
    try:
        from app.core.missions import MissionStore

        mission = MissionStore(ctx.database).unfinished()
    except Exception:
        return []
    if mission is None or mission.state != "paused":
        return []
    return [Candidate(f"mission-paused:{mission.id}", "mission", "Mission paused",
                      f"\"{mission.title}\" is paused ({mission.progress()}): {mission.note or 'waiting for you'}.",
                      0.6, 0.7, action="continue my mission")]


def _project_signals(ctx) -> list[Candidate]:
    try:
        from app.core.context_engine import ENGINE as CONTEXT

        history = CONTEXT.history()
    except Exception:
        return []
    if not history:
        return []
    snap = history[-1]
    changes, project = snap.value("git_changes"), snap.value("project")
    if project and isinstance(changes, int) and changes >= UNCOMMITTED_LIMIT:
        return [Candidate(f"uncommitted:{project}:{changes // 10}", "project", "Uncommitted work",
                          f"{project} has {changes} uncommitted changes. Want me to show the git status?",
                          0.35, 0.5, action="git status")]
    return []


def _failure_signals(ctx) -> list[Candidate]:
    try:
        from app.evolution.analyze import failures

        found = failures(ctx.database, days=2)
    except Exception:
        return []
    out = []
    for item in found[:2]:
        if item["count"] >= 5:
            out.append(Candidate(f"failing:{item['key']}", "system", "Something keeps failing",
                                 f"{item['tool']} failed {item['count']} times in two days ('{item['error']}'). Want me "
                                 "to look into it?", 0.4, 0.6, confidence=1.0, action="improve yourself"))
    return out


def scan(ctx, now: datetime) -> list[Decision]:
    """Consider every signal once; returns the decisions (the caller delivers speak/notify ones)."""
    decisions = [ENGINE.decide(c, ctx.settings, now) for c in candidates(ctx, now)]
    for decision in decisions:
        if decision.outcome != "drop":
            log_event(_log, "proactive", outcome=decision.outcome, category=decision.candidate.category,
                      score=decision.candidate.score)
    return decisions


def explain(decision: Decision) -> str:
    c = decision.candidate
    return (f"{c.title}: {c.message} - {decision.outcome} ({decision.reason}; urgency {c.urgency:.1f}, importance "
            f"{c.importance:.1f}, confidence {c.confidence:.1f}, interruption {c.interruption:.1f})")


def status(settings) -> dict[str, Any]:
    return {"level": getattr(settings, "proactive_level", "normal"), "quiet_hours": getattr(settings, "quiet_hours", ""),
            "max_per_hour": getattr(settings, "proactive_max_per_hour", 4),
            "categories": getattr(settings, "proactive_categories", "") or "all"}
