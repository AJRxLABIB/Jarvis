"""Sensor rules: "tell me if the office door has been open for 10 minutes", "tell me if the room is above 30 C" - NOTIFY ONLY.

``config/sensor_rules.json``::

    [{"name": "door left open", "sensor": "office door", "op": "=", "value": "open", "for_s": 600, "cooldown_min": 60, "message": "The office door has been open a while."},
     {"name": "room hot", "sensor": "room temperature", "op": ">", "value": 30, "for_s": 0, "cooldown_min": 120}]

Rules never act on devices (no switching, locking, heating): they only raise a notification, which goes through the attention firewall like any other.
A rule fires only from a FRESH, valid reading (``core/sensors``): stale, unknown or invalid readings never fire a rule and they reset the "for how long"
timer (a condition that cannot be confirmed is not "still true"). Each rule has a cooldown so it cannot nag. State lives in the settings store.
Pure ``evaluate`` over readings + state; ``run`` reads the broker and posts notifications. Not verified with real sensors.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.core import sensors as S

FILE = "sensor_rules.json"
STATE_KEY = "sensor_rules_state"
OPS = {">", "<", ">=", "<=", "="}
MAX_RULES = 40


@dataclass
class Rule:
    name: str
    sensor: str
    op: str
    value: Any
    for_s: int = 0
    cooldown_min: int = 60
    message: str = ""


@dataclass
class Alert:
    rule: Rule
    text: str


def load(config_dir: Path) -> list[Rule]:
    try:
        data = json.loads((Path(config_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rules = []
    for r in (data if isinstance(data, list) else [])[:MAX_RULES]:
        if not isinstance(r, dict) or r.get("op") not in OPS or not r.get("name") or not r.get("sensor") or "value" not in r:
            continue
        try:
            rules.append(Rule(str(r["name"])[:40], str(r["sensor"])[:40], r["op"], r["value"], max(0, min(int(r.get("for_s", 0)), 86400)),
                              max(1, min(int(r.get("cooldown_min", 60)), 10080)), str(r.get("message", ""))[:160]))
        except (TypeError, ValueError):
            continue
    return rules


def _holds(rule: Rule, reading: S.Reading) -> bool:
    v = reading.value
    if isinstance(rule.value, str) or isinstance(v, str):
        return rule.op == "=" and str(v).lower() == str(rule.value).lower()
    try:
        w = float(rule.value)
    except (TypeError, ValueError):
        return False
    return {">": v > w, "<": v < w, ">=": v >= w, "<=": v <= w, "=": v == w}[rule.op]


def evaluate(rules: list[Rule], readings: list[S.Reading], state: dict[str, dict], now: datetime) -> list[Alert]:
    """Update ``state`` in place ({rule: {since, fired}}) and return the alerts that fire now."""
    by_name = {r.sensor.name.lower(): r for r in readings}
    alerts = []
    for rule in rules:
        st = state.setdefault(rule.name, {})
        reading = by_name.get(rule.sensor.lower())
        if reading is None or reading.status != "ok" or not _holds(rule, reading):
            st["since"] = None                                   # cannot confirm it: the timer starts over
            continue
        since = datetime.fromisoformat(st["since"]) if st.get("since") else now
        st["since"] = since.isoformat()
        if (now - since).total_seconds() < rule.for_s:
            continue
        fired = datetime.fromisoformat(st["fired"]) if st.get("fired") else None
        if fired and now - fired < timedelta(minutes=rule.cooldown_min):
            continue
        st["fired"] = now.isoformat()
        alerts.append(Alert(rule, rule.message or f"{rule.sensor}: {reading.text()}"))
    return alerts


def run(db: Any, store: Any, config_dir: Path, now: datetime | None = None, reader: Callable[..., tuple[list[S.Reading], str]] = S.read_all) -> list[Alert]:
    rules = load(config_dir)
    if not rules:
        return []
    now = now or datetime.now(timezone.utc)
    readings, _problem = reader(config_dir, 2.0)
    state = dict(store.get(STATE_KEY, {}) or {})
    alerts = evaluate(rules, readings, state, now)
    store.set(STATE_KEY, state)
    if alerts:
        from app.core.notifications import NotificationCenter

        center = NotificationCenter(db)
        for a in alerts:
            center.add(f"Sensor: {a.rule.name}", a.text, source="sensors", category="system", priority="normal")
    return alerts


def due(last: float | None, every_s: int = 120, now: float | None = None) -> bool:
    return last is None or (time.monotonic() if now is None else now) - last >= every_s
