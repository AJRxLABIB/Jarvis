"""Statistical anomaly detection - "this is unusual for YOU", not a fixed threshold.

Everything is local, read-only and deterministic (no model). Each check compares a recent window with your own earlier
history using robust statistics (median and MAD), so one earlier spike doesn't hide the next one and a noisy series
doesn't cry wolf:

* tool latency      - a tool that is suddenly much slower than it normally is (needs ``elapsed_ms`` in the audit log,
                      recorded from this version on; older rows simply don't count)
* tool failures     - a tool that fails far more often than its own history makes plausible (binomial tail)
* PC load           - memory / CPU in the last hour against the past two weeks (``metrics`` table)
* spending          - an expense far above that category's usual amounts (``expenses`` table)

Honest limits: too little history means "no verdict" (never a guess); the score says how unusual, not why; a flagged
value can be perfectly fine (you bought something big). Alerts go through the radar, so snooze / mute / "not useful"
apply and three "not useful" ratings mute one for a week.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

MIN_BASELINE = 8             # earlier samples needed before anything can be called unusual
MIN_RECENT = 3               # recent samples needed (latency / failures)
Z_LIMIT = 3.5                # robust z-score above which a value is flagged (Iglewicz-Hoaglin)
RATIO_LIMIT = 1.5            # ... and it must also be this many times the usual value (ignores tiny absolute wobble)
FAIL_P_LIMIT = 0.01          # chance of seeing this many failures if the old failure rate still held
RECENT_HOURS = 24
BASELINE_DAYS = 30


@dataclass(frozen=True)
class Anomaly:
    key: str
    kind: str                # latency | failures | load | spend
    subject: str
    observed: float
    usual: float
    score: float             # robust z for numeric checks; -log10(p) for failures
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "subject": self.subject, "observed": round(self.observed, 2),
                "usual": round(self.usual, 2), "score": round(self.score, 1), "message": self.message}


# ------------------------------------------------------------------ the statistics


def robust_z(value: float, history: list[float]) -> float | None:
    """How many 'typical deviations' ``value`` is above the history's median. None when there is too little history.

    Uses 0.6745 * (x - median) / MAD. When the history is (nearly) constant, MAD is 0; a floor of 5 % of the median
    (or 1e-9) stops a tiny change from scoring as infinite, so a perfectly steady series needs a real jump to flag."""
    if len(history) < MIN_BASELINE:
        return None
    mid = median(history)
    mad = median(abs(x - mid) for x in history)
    floor = max(abs(mid) * 0.05, 1e-9)
    return 0.6745 * (value - mid) / max(mad, floor)


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p): how likely at least ``k`` failures in ``n`` calls are at the old failure rate."""
    if k <= 0:
        return 1.0
    p = min(max(p, 1e-6), 1 - 1e-6)
    total = 0.0
    for i in range(k, n + 1):
        total += math.comb(n, i) * p**i * (1 - p) ** (n - i)
    return min(1.0, total)


def _utc(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ------------------------------------------------------------------ checks


def tool_anomalies(db: Any, now: datetime | None = None) -> list[Anomaly]:
    now = _utc(now)
    cut = _iso(now - timedelta(hours=RECENT_HOURS))
    start = _iso(now - timedelta(days=BASELINE_DAYS))
    with db.connect() as conn:
        rows = conn.execute("SELECT ts, tool, details FROM audit_log WHERE event = 'tool_result' AND ts >= ? "
                            "AND tool IS NOT NULL", (start,)).fetchall()
    old: dict[str, list[tuple[bool, float | None]]] = {}
    new: dict[str, list[tuple[bool, float | None]]] = {}
    for ts, tool, details in rows:
        try:
            data = json.loads(details or "{}")
        except ValueError:
            continue
        if data.get("error") in {"user_declined", "confirmation_denied", "blocked", "emergency_stop", "rate_limited",
                                 "unknown_tool", "invalid_arguments"}:
            continue                    # refused or malformed calls say nothing about how the tool itself behaves
        ms = data.get("elapsed_ms")
        item = (bool(data.get("ok")), float(ms) if isinstance(ms, (int, float)) else None)
        (new if ts >= cut else old).setdefault(tool, []).append(item)
    found: list[Anomaly] = []
    for tool, recent in sorted(new.items()):
        history = old.get(tool, [])
        times_old = [m for ok, m in history if ok and m is not None]
        times_new = [m for ok, m in recent if ok and m is not None]
        if len(times_new) >= MIN_RECENT:
            usual, now_med = median(times_old) if times_old else 0.0, median(times_new)
            z = robust_z(now_med, times_old)
            if z is not None and z >= Z_LIMIT and now_med >= RATIO_LIMIT * usual and now_med - usual >= 200:
                found.append(Anomaly(f"anomaly:latency:{tool}", "latency", tool, now_med, usual, z,
                                     f"{tool} is slower than usual: about {now_med / 1000:.1f} s now against "
                                     f"{usual / 1000:.1f} s normally ({len(times_new)} calls today, {len(times_old)} before)."))
        if len(history) >= MIN_BASELINE and len(recent) >= MIN_RECENT:
            base_rate = sum(1 for ok, _ in history if not ok) / len(history)
            fails = sum(1 for ok, _ in recent if not ok)
            p = binomial_tail(fails, len(recent), base_rate)
            if fails >= 2 and fails / len(recent) >= 0.5 and p < FAIL_P_LIMIT:
                found.append(Anomaly(f"anomaly:failures:{tool}", "failures", tool, fails / len(recent), base_rate, -math.log10(p),
                                     f"{tool} failed {fails} of {len(recent)} times today; it normally fails "
                                     f"{base_rate:.0%} of the time. Something may have changed (a setting, an app, the network)."))
    return found


def load_anomalies(db: Any, now: datetime | None = None) -> list[Anomaly]:
    now = _utc(now)
    found: list[Anomaly] = []
    with db.connect() as conn:
        rows = conn.execute("SELECT ts, cpu, mem_percent FROM metrics WHERE ts >= ? ORDER BY ts",
                            (_iso(now - timedelta(days=14)),)).fetchall()
    recent_cut = _iso(now - timedelta(hours=1))
    for column, label in ((2, "Memory use"), (1, "CPU use")):
        old = [r[column] for r in rows if r[0] < recent_cut and r[column] is not None]
        new = [r[column] for r in rows if r[0] >= recent_cut and r[column] is not None]
        if len(new) < MIN_RECENT:
            continue
        value, usual = median(new), median(old) if old else 0.0
        z = robust_z(value, old)
        if z is not None and z >= Z_LIMIT and value - usual >= 15:
            found.append(Anomaly(f"anomaly:load:{label.split()[0].lower()}", "load", label, value, usual, z,
                                 f"{label} has been about {value:.0f}% for the last hour; over the past two weeks it is "
                                 f"usually about {usual:.0f}%."))
    return found


def spend_anomalies(db: Any, now: datetime | None = None, days: int = 7) -> list[Anomaly]:
    now = _utc(now)
    cut = _iso(now - timedelta(days=days))
    start = _iso(now - timedelta(days=180))
    with db.connect() as conn:
        rows = conn.execute("SELECT id, ts, amount, category, currency FROM expenses WHERE ts >= ?", (start,)).fetchall()
    by_cat: dict[str, list[tuple[str, float, int, str]]] = {}
    for rid, ts, amount, category, currency in rows:
        by_cat.setdefault(category, []).append((ts, float(amount), rid, currency))
    found = []
    for category, items in sorted(by_cat.items()):
        old = [a for ts, a, _, _ in items if ts < cut]
        for _ts, amount, rid, currency in sorted(i for i in items if i[0] >= cut):
            z = robust_z(amount, old)
            usual = median(old) if old else 0.0
            if z is not None and z >= Z_LIMIT and amount >= 2 * usual:
                found.append(Anomaly(f"anomaly:spend:{rid}", "spend", category, amount, usual, z,
                                     f"A {category} expense of {amount:g} {currency}".rstrip() +
                                     f" is far above your usual {usual:g} for that category ({len(old)} earlier entries). "
                                     "If it was intended, ignore this."))
    return found


def detect(db: Any, now: datetime | None = None) -> list[Anomaly]:
    out: list[Anomaly] = []
    for check in (tool_anomalies, load_anomalies, spend_anomalies):
        try:
            out += check(db, now)
        except Exception:               # one broken source must not hide the others
            continue
    return sorted(out, key=lambda a: -a.score)


def describe(found: list[Anomaly], days: int = BASELINE_DAYS) -> str:
    if not found:
        return (f"Nothing looks unusual against your own last {days} days. (With too little history I say nothing rather "
                "than guess: at least 8 earlier samples are needed per check.)")
    return "\n".join(f"- {a.message} [{a.kind}, score {a.score:.1f}]" for a in found)


# ------------------------------------------------------------------ radar hook


def signals(ctx: Any, now: datetime | None = None) -> list[Any]:
    from app.core.proactive import Candidate

    out = []
    for a in detect(ctx.database, now)[:5]:
        out.append(Candidate(a.key, "system", "Something looks unusual", a.message,
                             urgency=0.4, importance=0.5, confidence=min(0.9, 0.5 + a.score / 20), interruption=0.3))
    return out
