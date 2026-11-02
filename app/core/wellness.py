"""Optional wellness log: sleep, exercise, breaks, energy and your own goals - stored locally, separately permissioned, off by default.

Nothing is collected unless you switch it on and tell JARVIS the numbers; it never reads the camera, microphone, location or any sensor for
this. Observations are plain statistics with their working shown (averages, a comparison of the last week with the one before, and
whether days after better sleep were also higher-energy days once there are at least 10 paired days). They are patterns in YOUR numbers,
not medical advice and not diagnoses. Habit check-ins stay in the existing habit tracker. ``delete_all`` removes everything.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

KEY, ENABLED = "wellness.log", "wellness.enabled"
METRICS = {"sleep_hours": (0, 24), "exercise_min": (0, 600), "breaks": (0, 50), "energy": (1, 5), "mood": (1, 5), "water_glasses": (0, 40)}
MAX_DAYS = 800
MIN_PAIRED = 10
DISCLAIMER = "These are patterns in your own numbers, not medical advice."


def enabled(store: Any) -> bool:
    return bool(store.get(ENABLED, False))


def set_enabled(store: Any, on: bool) -> None:
    store.set(ENABLED, bool(on))


def log(store: Any, metric: str, value: float, day: date | None = None) -> str:
    if not enabled(store):
        raise PermissionError("Wellness tracking is off. Turn it on first; nothing is recorded until you do.")
    if metric not in METRICS:
        raise ValueError("Metrics: " + ", ".join(METRICS))
    lo, hi = METRICS[metric]
    if not lo <= value <= hi:
        raise ValueError(f"{metric} must be between {lo} and {hi}.")
    data = store.get(KEY, {}) or {}
    key = (day or date.today()).isoformat()
    data.setdefault(key, {})[metric] = float(value)
    for old in sorted(data)[:-MAX_DAYS]:
        del data[old]
    store.set(KEY, data)
    return key


def _series(data: dict[str, dict[str, float]], metric: str, start: date, end: date) -> list[float]:
    out, d = [], start
    while d <= end:
        v = data.get(d.isoformat(), {}).get(metric)
        if v is not None:
            out.append(v)
        d += timedelta(days=1)
    return out


def summary(store: Any, today: date | None = None, days: int = 7) -> str:
    if not enabled(store):
        return "Wellness tracking is off."
    today = today or date.today()
    data = store.get(KEY, {}) or {}
    lines = []
    for metric in METRICS:
        now = _series(data, metric, today - timedelta(days=days - 1), today)
        if not now:
            continue
        before = _series(data, metric, today - timedelta(days=2 * days - 1), today - timedelta(days=days))
        line = f"{metric.replace('_', ' ')}: average {statistics.mean(now):.1f} over {len(now)} day(s)"
        if before:
            diff = statistics.mean(now) - statistics.mean(before)
            line += f" ({'+' if diff >= 0 else ''}{diff:.1f} vs the {len(before)} day(s) before)"
        lines.append(line)
    return ("Last %d days - " % days + "; ".join(lines) + ". " + DISCLAIMER) if lines else "Nothing logged in the last %d days." % days


def insights(store: Any, today: date | None = None) -> list[str]:
    """Explainable observations with their working; empty when there is not enough data (never a guess)."""
    if not enabled(store):
        return []
    today = today or date.today()
    data = store.get(KEY, {}) or {}
    out = []
    pairs = []
    for key, row in data.items():
        d = date.fromisoformat(key)
        nxt = data.get((d + timedelta(days=1)).isoformat(), {})
        if "sleep_hours" in row and "energy" in nxt:
            pairs.append((row["sleep_hours"], nxt["energy"]))
    if len(pairs) >= MIN_PAIRED:
        median = statistics.median(p[0] for p in pairs)
        high = [e for s, e in pairs if s >= median]
        low = [e for s, e in pairs if s < median]
        if high and low:
            diff = statistics.mean(high) - statistics.mean(low)
            out.append(f"On the {len(high)} day(s) after sleeping {median:.1f} h or more your next-day energy averaged {statistics.mean(high):.1f}, "
                       f"against {statistics.mean(low):.1f} after shorter nights ({'+' if diff >= 0 else ''}{diff:.1f}). {len(pairs)} paired days; "
                       "this shows a pattern, not a cause.")
    recent = _series(data, "breaks", today - timedelta(days=6), today)
    if len(recent) >= 3 and statistics.mean(recent) < 1:
        out.append(f"You logged fewer than 1 break a day on average over {len(recent)} day(s).")
    if not out:
        out.append(f"Not enough data for patterns yet (needs {MIN_PAIRED}+ days of sleep and next-day energy).")
    return out


def delete_all(store: Any) -> None:
    store.set(KEY, {})
