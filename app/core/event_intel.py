"""Event intelligence: turn the raw event log into ONE explained situation instead of several unrelated alerts.

The event bus (``core/events.py``) already announces and stores events. This module reads them and adds what the bus does not:

* ``normalise``  - every event gets the full schema: category, source, device, time, related mission / project / run, evidence, confidence,
  priority, privacy class, expiry and possible responses (a fixed table; unknown names are kept as "other", never dropped);
* ``correlate``  - events that belong together are grouped: the same run / mission / project, or problems of the same device close in time;
* ``diagnose``   - one summary per group: what happened first, what changed before it (injected readers, e.g. git), whether it has happened
  before, and next steps as TEXT. Confidence is "high" only when a log or exit code was actually captured; otherwise the cause is labelled a guess;
* ``decide``     - notify now / hold for the digest / ignore, using the attention rules (``core/attention.py``) and never the same group twice.

Nothing here acts: responses are suggestions, and each one still goes through the normal tools and approvals. Event data is untrusted text:
it is only printed or matched against fixed rules, never run. Pure functions over event dicts + injected readers (tests use made-up events).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

PROBLEM_NAMES = {"PcProblem", "SecurityEvent", "BuildFailed", "BatteryLow", "Emergency"}
WINDOW = timedelta(minutes=10)
# name -> (category, priority, privacy, possible responses, expires in hours)
TABLE: dict[str, tuple[str, str, str, tuple[str, ...], int]] = {
    "RunFinished": ("task", "low", "private", ("show the run timeline",), 24),
    "MissionCompleted": ("mission", "normal", "private", ("show the result",), 72),
    "MissionPaused": ("mission", "high", "private", ("resume the mission", "show why it paused"), 72),
    "BatteryLow": ("device", "high", "internal", ("save work", "switch to battery saver"), 6),
    "PcProblem": ("system", "high", "internal", ("run diagnostics", "show resource use"), 24),
    "DownloadCompleted": ("file", "low", "private", ("open the download",), 24),
    "Reminder": ("reminder", "normal", "private", ("snooze", "mark done"), 12),
    "Suggestion": ("suggestion", "low", "private", ("accept", "dismiss"), 12),
    "SecurityEvent": ("security", "urgent", "sensitive", ("review the incident", "check paired devices"), 168),
    "Emergency": ("security", "urgent", "internal", ("review the audit log",), 168),
    "JarvisUpdated": ("system", "low", "public", ("show what changed",), 48),
    "ModeChanged": ("system", "low", "internal", (), 6),
    "BuildFailed": ("build", "high", "private", ("show the log tail", "explain the error", "show recent changes"), 48),
}


@dataclass
class Intel:
    id: str
    name: str
    category: str
    source: str
    device: str
    ts: str
    mission: str
    project: str
    run: str
    evidence: dict[str, Any]
    confidence: str
    priority: str
    privacy: str
    expires: str
    responses: tuple[str, ...]

    def when(self) -> datetime:
        try:
            return datetime.fromisoformat(self.ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)

    def expired(self, now: datetime) -> bool:
        try:
            return datetime.fromisoformat(self.expires) < now
        except ValueError:
            return False


def normalise(row: dict[str, Any]) -> Intel:
    """``row``: a stored event (id, ts, name, run_id, data as JSON text or dict)."""
    data = row.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    category, priority, privacy, responses, hours = TABLE.get(row.get("name", ""), ("other", "low", "private", (), 24))
    ts = str(row.get("ts") or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    base = Intel(id=str(row.get("id", "")), name=str(row.get("name", "")), category=category,
                 source=str(data.get("source") or category), device=str(data.get("device") or "pc"), ts=ts,
                 mission=str(data.get("mission") or data.get("mission_id") or ""), project=str(data.get("project") or ""),
                 run=str(row.get("run_id") or ""), evidence={k: v for k, v in data.items() if k not in ("source", "device")},
                 confidence="high" if data.get("log") or data.get("exit_code") is not None else "medium", priority=str(data.get("priority") or priority),
                 privacy=privacy, expires="", responses=responses)
    base.expires = (base.when() + timedelta(hours=hours)).isoformat(timespec="seconds")
    return base


def _related(a: Intel, b: Intel) -> bool:
    if a.run and a.run == b.run:
        return True
    if a.mission and a.mission == b.mission:
        return True
    if a.project and a.project == b.project and abs(a.when() - b.when()) <= WINDOW * 3:
        return True
    return a.name in PROBLEM_NAMES and b.name in PROBLEM_NAMES and a.device == b.device and abs(a.when() - b.when()) <= WINDOW


def correlate(events: list[Intel], now: datetime | None = None) -> list[list[Intel]]:
    """Groups of related, not-yet-expired events, oldest first inside a group, newest group first. Ignorable noise (ModeChanged) is left out."""
    now = now or datetime.now(timezone.utc)
    live = sorted((e for e in events if not e.expired(now) and e.name != "ModeChanged"), key=lambda e: e.when())
    groups: list[list[Intel]] = []
    for e in live:
        placed = [g for g in groups if any(_related(e, x) for x in g)]
        if not placed:
            groups.append([e])
            continue
        placed[0].append(e)
        for extra in placed[1:]:                      # an event that links two groups merges them
            placed[0].extend(extra)
            groups.remove(extra)
        placed[0].sort(key=lambda x: x.when())
    return sorted(groups, key=lambda g: g[-1].when(), reverse=True)


@dataclass
class Context:
    """Readers for what changed around the event. All optional: a missing reader just means that evidence is not available (and is said so)."""

    changes_before: Callable[[str, datetime], list[str]] = lambda project, when: []      # e.g. git: files changed since the last good build
    seen_before: Callable[[str], int] = lambda signature: 0                                # how many earlier events had this signature


@dataclass
class Diagnosis:
    key: str
    title: str
    priority: str
    confidence: str
    lines: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def text(self) -> str:
        out = [f"{self.title} [{self.priority}, confidence {self.confidence}]", *[f"  - {x}" for x in self.lines]]
        if self.gaps:
            out.append("  Not available: " + "; ".join(self.gaps))
        if self.steps:
            out.append("  Suggested: " + "; ".join(self.steps))
        return "\n".join(out)


_ORDER = {"low": 0, "normal": 1, "high": 2, "urgent": 3}


def signature(group: list[Intel]) -> str:
    first = group[0]
    return f"{first.category}:{first.project or first.mission or first.device}:{first.name}"


def diagnose(group: list[Intel], ctx: Context | None = None) -> Diagnosis:
    ctx = ctx or Context()
    group = sorted(group, key=lambda e: e.when())
    first = group[0]
    priority = max((e.priority for e in group), key=lambda p: _ORDER.get(p, 1))
    names = ", ".join(dict.fromkeys(e.name for e in group))
    d = Diagnosis(key=signature(group), title=f"{first.category} situation on {first.device}: {names}", priority=priority,
                  confidence=max((e.confidence for e in group), key={"low": 0, "medium": 1, "high": 2}.get))      # the best-evidenced event backs the diagnosis
    d.lines.append(f"First: {first.name} at {first.ts[11:19] or first.ts}" + (f" (project {first.project})" if first.project else ""))
    if len(group) > 1:
        d.lines.append(f"{len(group) - 1} related event(s) followed; they are reported together, not as separate alerts.")
    log = next((str(e.evidence.get("log") or e.evidence.get("error") or "") for e in group if e.evidence.get("log") or e.evidence.get("error")), "")
    if log:
        d.lines.append("Evidence: " + log.strip().splitlines()[-1][:200])
    else:
        d.gaps.append("no log or error text was captured, so the cause below is a guess")
        d.confidence = "low" if d.confidence == "medium" else d.confidence
    if first.name == "BuildFailed" or any(e.name == "BuildFailed" for e in group):
        where = str(next((e.evidence.get("folder") for e in group if e.evidence.get("folder")), "") or first.project)
        changes = ctx.changes_before(where, first.when()) if where else []
        if changes:
            d.lines.append(f"Changed before it: {', '.join(changes[:6])}" + (f" (+{len(changes) - 6} more)" if len(changes) > 6 else ""))
        else:
            d.gaps.append("what changed before the failure (no project or version-control information)")
    times = ctx.seen_before(signature(group))
    if times:
        d.lines.append(f"This kind of event happened {times} time(s) before.")
    d.steps = list(dict.fromkeys(r for e in group for r in e.responses))[:4]
    return d


@dataclass
class Decision:
    action: str            # notify | digest | ignore
    why: str


def decide(d: Diagnosis, *, reported: set[str], attention: Callable[[str, str], tuple[str, str]]) -> Decision:
    """``attention(priority, category)`` -> ("now" | "digest" | "hold", reason). A situation already reported is not repeated."""
    if d.key in reported:
        return Decision("ignore", "already reported")
    when, why = attention(d.priority, d.key.split(":", 1)[0])
    return Decision({"now": "notify", "digest": "digest", "hold": "digest"}[when], why)


def recent(db: Any, limit: int = 200) -> list[Intel]:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]
    return [normalise(r) for r in rows]


def situations(db: Any, ctx: Context | None = None, now: datetime | None = None) -> list[Diagnosis]:
    return [diagnose(g, ctx) for g in correlate(recent(db), now)]
