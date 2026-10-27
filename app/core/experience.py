"""The Experience Compiler - turns what you keep doing into a workflow you can approve.

Teach-by-showing (app/automation/teach.py) needs you to announce a routine. This finds routines you never announced:
sequences of successful tool calls that Jarvis carried out for you, in the same order, on at least two different days
(``MIN_DAYS``) and at least three times (``MIN_RUNS``), from the audit log. Each becomes a PROPOSAL with its steps, how often
and when it last happened, which arguments were the same every time (kept) and which changed (flagged - the latest value
is used and you are told it varied), and a rough time saving from the real durations.

What it deliberately is not:
* not a recorder: it reads only the audit trail Jarvis already keeps of its own tool calls (never keyboard, mouse,
  screen, microphone or files), and only successful calls;
* not autonomous: nothing is saved, scheduled or run here. Saving goes through the existing workflow path, with the same
  step validation, and always asks you on screen;
* never includes tools that automations may not use, risky (dangerous / critical) calls, calls whose recorded arguments
  held anything secret-looking, or calls that were refused, cancelled or failed;
* a proposal you dismiss is not offered again.

Limits: it sees only calls that went through Jarvis; "same order" means consecutive successful calls within ten minutes
of each other; at most ``MAX_STEPS`` steps; the time saving is an estimate from past durations, not a promise.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from app.automation.tasks import MAX_STEPS
from app.automation.teach import NEVER_RECORDED
from app.memory.settings_store import SettingsStore
from app.security.permissions import RiskLevel
from app.utils.redaction import REDACTED, redact

MIN_RUNS = 3
MIN_DAYS = 2
MIN_STEPS = 2
GAP_MINUTES = 10
LOOKBACK_DAYS = 30
DISMISSED_KEY = "experience.dismissed"


@dataclass
class Proposal:
    id: str                                   # stable: the tool names joined, so a dismissal survives re-mining
    name: str
    steps: list[dict[str, Any]]
    runs: int
    days: int
    last_seen: str
    varied: list[str] = field(default_factory=list)     # arguments that changed and could NOT become inputs
    seconds_saved: float = 0.0
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)   # named inputs made from text that changed between runs

    def describe(self) -> str:
        steps = " -> ".join(s["tool"] + (f"({', '.join(f'{k}={v!r}' for k, v in s['arguments'].items())})" if s["arguments"] else "")
                            for s in self.steps)
        note = f" Changed between runs (latest value used): {'; '.join(self.varied)}." if self.varied else ""
        if self.parameters:
            note += " Inputs: " + "; ".join(f"{k} (usually {p['description']})" for k, p in self.parameters.items()) + "."
        saved = f" Roughly {self.seconds_saved:.0f} s of tool time each run." if self.seconds_saved >= 5 else ""
        return (f"'{self.name}': {steps}. Seen {self.runs} times on {self.days} days, last {self.last_seen[:10]}.{note}{saved}")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "steps": self.steps, "runs": self.runs, "days": self.days,
                "last_seen": self.last_seen, "varied": self.varied, "parameters": self.parameters}


@dataclass
class _Call:
    ts: datetime
    tool: str
    arguments: dict[str, Any]
    ms: float | None


# ------------------------------------------------------------------ reading the trail


def _eligible(tool: str, level: int | None, arguments: Any) -> bool:
    if not tool or tool in NEVER_RECORDED:
        return False
    if level is not None and level >= int(RiskLevel.DANGEROUS):
        return False
    if not isinstance(arguments, dict):
        return False
    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return False
    return REDACTED not in text and redact(text) == text


def load(db: Any, now: datetime | None = None, days: int = LOOKBACK_DAYS) -> list[list[_Call]]:
    """Successful, eligible calls grouped into sessions (a pause of more than ``GAP_MINUTES`` starts a new one). An
    ineligible or failed call also ends the session: 'A, <something else>, B' is not the sequence 'A, B'."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as conn:
        rows = conn.execute("SELECT ts, tool, permission_level, details FROM audit_log WHERE event = 'tool_result' "
                            "AND ts >= ? ORDER BY ts, id", (since,)).fetchall()
    sessions: list[list[_Call]] = []
    current: list[_Call] = []
    last: datetime | None = None
    for ts, tool, level, details in rows:
        try:
            data = json.loads(details or "{}")
            at = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if last is not None and at - last > timedelta(minutes=GAP_MINUTES):
            sessions.append(current)
            current = []
        last = at
        arguments = data.get("arguments")
        if not data.get("ok") or not _eligible(tool, level, arguments):
            if current:
                sessions.append(current)
                current = []
            continue
        ms = data.get("elapsed_ms")
        current.append(_Call(at, tool, arguments, float(ms) if isinstance(ms, (int, float)) else None))
    if current:
        sessions.append(current)
    return [s for s in sessions if len(s) >= MIN_STEPS]


# ------------------------------------------------------------------ mining


def _occurrences(sessions: list[list[_Call]], pattern: tuple[str, ...]) -> list[list[_Call]]:
    """Non-overlapping occurrences of the tool-name sequence ``pattern`` across the sessions."""
    n, found = len(pattern), []
    for session in sessions:
        i = 0
        while i + n <= len(session):
            if tuple(c.tool for c in session[i:i + n]) == pattern:
                found.append(session[i:i + n])
                i += n
            else:
                i += 1
    return found


def _name(steps: list[dict[str, Any]]) -> str:
    words = []
    for s in steps[:3]:
        label = re.sub(r"[^A-Za-z0-9 ]", "", str(next(iter(s["arguments"].values()), "")))[:14].strip()
        words.append(f"{s['tool'].replace('_', ' ')}{' ' + label if label else ''}")
    return ("Routine: " + ", ".join(words))[:60]


def _parametrize(tool: str, position: int, occ: list[list[_Call]], arguments: dict[str, Any],
                 parameters: dict[str, dict[str, Any]]) -> set[str]:
    """Turn arguments that are TEXT and differed between runs into named inputs ({{name}}); returns the keys it converted.
    The saved default is the latest value, the description lists a few real values. At most MAX_PARAMS inputs per routine;
    numbers, lists and anything secret-looking stay fixed (and are reported)."""
    from app.automation.tasks import MAX_PARAMETERS

    made: set[str] = set()
    for key, latest in list(arguments.items()):
        if len(parameters) >= MAX_PARAMETERS or not isinstance(latest, str) or not latest.strip():
            continue
        values = [o[position].arguments.get(key) for o in occ]
        if not all(isinstance(v, str) and v.strip() for v in values) or len(set(values)) < 2:
            continue
        if any(len(v) > 200 or "{{" in v or redact(v) != v for v in values):
            continue
        base = re.sub(r"[^a-z0-9_]", "", key.lower())[:24] or "value"
        if not base[0].isalpha():
            base = "v" + base
        name = base if base not in parameters else f"{base}{position + 1}"
        if name in parameters:
            continue
        seen = list(dict.fromkeys(values))[:3]
        parameters[name] = {"default": latest, "description": f"{tool} {key}, e.g. " + ", ".join(repr(v[:40]) for v in seen)}
        arguments[key] = "{{" + name + "}}"
        made.add(key)
    return made


def mine(sessions: list[list[_Call]]) -> list[Proposal]:
    seen: set[tuple[str, ...]] = set()
    candidates: dict[tuple[str, ...], list[list[_Call]]] = {}
    for session in sessions:
        for n in range(MIN_STEPS, MAX_STEPS + 1):
            for i in range(len(session) - n + 1):
                seen.add(tuple(c.tool for c in session[i:i + n]))
    for pattern in seen:
        occ = _occurrences(sessions, pattern)
        days = {o[0].ts.date() for o in occ}
        if len(occ) >= MIN_RUNS and len(days) >= MIN_DAYS:
            candidates[pattern] = occ
    # keep maximal patterns: drop one that sits inside a longer pattern seen at least as often
    keep = {}
    for pattern, occ in candidates.items():
        inside = any(len(other) > len(pattern) and len(candidates[other]) >= len(occ)
                     and any(other[i:i + len(pattern)] == pattern for i in range(len(other) - len(pattern) + 1))
                     for other in candidates)
        if not inside:
            keep[pattern] = occ
    proposals = []
    for pattern, occ in keep.items():
        steps, varied, saved, parameters = [], [], 0.0, {}
        for position, tool in enumerate(pattern):
            versions = [json.dumps(o[position].arguments, sort_keys=True, default=str) for o in occ]
            latest = max(occ, key=lambda o: o[0].ts)[position].arguments
            arguments = dict(latest)
            if len(set(versions)) > 1:
                made = _parametrize(tool, position, occ, arguments, parameters)
                rest = [k for k in latest if k not in made and len({json.dumps(o[position].arguments.get(k), default=str) for o in occ}) > 1]
                if rest:
                    varied.append(f"step {position + 1} {tool}: {', '.join(rest)} changed and stays fixed at the latest value")
            steps.append({"tool": tool, "arguments": arguments})
            times = [o[position].ms for o in occ if o[position].ms is not None]
            saved += median(times) / 1000 if times else 0.0
        if len({json.dumps(s, sort_keys=True) for s in steps}) < 2:
            continue                                   # the same step repeated is a loop, not a routine
        proposals.append(Proposal(
            id="+".join(pattern), name=_name(steps), steps=steps, runs=len(occ), days=len({o[0].ts.date() for o in occ}),
            last_seen=max(o[-1].ts for o in occ).isoformat(timespec="seconds"), varied=varied, seconds_saved=saved,
            parameters=parameters))
    proposals.sort(key=lambda p: (-(p.runs * len(p.steps)), p.id))
    return proposals


# ------------------------------------------------------------------ dismissals and existing workflows


def dismissed(db: Any) -> set[str]:
    value = SettingsStore(db).get(DISMISSED_KEY, [])
    return {str(v) for v in value} if isinstance(value, list) else set()


def dismiss(db: Any, proposal_id: str) -> None:
    SettingsStore(db).set(DISMISSED_KEY, sorted(dismissed(db) | {proposal_id})[:200])


def propose(db: Any, now: datetime | None = None, days: int = LOOKBACK_DAYS, existing_steps: list[list[str]] | None = None) -> list[Proposal]:
    """Proposals not dismissed and not already covered by a saved workflow with the same tool sequence."""
    skip = dismissed(db)
    have = {tuple(s) for s in (existing_steps or [])}
    return [p for p in mine(load(db, now, days)) if p.id not in skip and tuple(p.id.split("+")) not in have][:10]
