"""PC health history and diagnosis: a light record of CPU / memory / disk / battery, and plain-language findings.

* ``MetricsStore.record`` keeps at most one row a minute and forgets rows older than the retention (14 days), so it
  costs almost nothing. Values the PC doesn't report (no battery, no sensor) are stored as NULL and shown as
  "not available" - never invented.
* ``diagnose`` reads the history plus what Jarvis knows (failed automations, paused or stuck missions, models that
  keep failing) and returns findings with the evidence and a suggested next step. It only reads and advises: it never
  ends a process, changes a setting or deletes a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

RECORD_EVERY = timedelta(seconds=55)
RETENTION = timedelta(days=14)

HIGH_CPU, HIGH_CPU_SAMPLES = 90.0, 5          # >= 90 % for 5 minutes in a row
LOW_DISK_PERCENT = 90.0                       # more than 90 % full
LOW_BATTERY = 20.0
MEMORY_HIGH = 85.0
MEMORY_GROWTH_PER_HOUR = 5.0                  # percentage points an hour, over at least 30 minutes of history


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def sample_now(drive: str = "C:\\") -> dict[str, Any]:
    """One reading from psutil; anything unavailable is None."""
    import psutil

    reading: dict[str, Any] = {"cpu": None, "mem_percent": None, "disk_percent": None, "battery": None, "plugged": None}
    try:
        reading["cpu"] = float(psutil.cpu_percent(None))
        reading["mem_percent"] = float(psutil.virtual_memory().percent)
    except Exception:
        pass
    try:
        reading["disk_percent"] = float(psutil.disk_usage(drive).percent)
    except Exception:
        pass
    try:
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if battery is not None:
            reading["battery"], reading["plugged"] = float(battery.percent), bool(battery.power_plugged)
    except Exception:
        pass
    return reading


class MetricsStore:
    def __init__(self, database, retention: timedelta = RETENTION) -> None:
        self._db = database
        self.retention = retention

    def record(self, reading: dict[str, Any], now: datetime | None = None) -> bool:
        """Store a reading unless one was stored in the last minute. Returns whether it was stored."""
        now = now or datetime.now(timezone.utc)
        with self._db.connect() as conn:
            last = conn.execute("SELECT MAX(ts) FROM metrics").fetchone()[0]
        if last and now - datetime.fromisoformat(last) < RECORD_EVERY:
            return False
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO metrics (ts, cpu, mem_percent, disk_percent, battery, plugged) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (_iso(now), reading.get("cpu"), reading.get("mem_percent"), reading.get("disk_percent"),
                          reading.get("battery"), None if reading.get("plugged") is None else int(reading["plugged"])))
            conn.execute("DELETE FROM metrics WHERE ts < ?", (_iso(now - self.retention),))
        return True

    def history(self, hours: float = 24, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM metrics WHERE ts >= ? ORDER BY ts",
                                (_iso(now - timedelta(hours=hours)),)).fetchall()
        return [dict(r) for r in rows]

    def delete(self, older_than_days: int | None = None, now: datetime | None = None) -> int:
        """Forget readings: all of them, or only those older than N days. Returns how many were removed."""
        with self._db.transaction() as conn:
            if older_than_days is None:
                return conn.execute("DELETE FROM metrics").rowcount
            cutoff = _iso((now or datetime.now(timezone.utc)) - timedelta(days=older_than_days))
            return conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,)).rowcount


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str  # info | warning | problem
    message: str
    evidence: str
    suggestion: str
    basis: str = "observed"   # "observed" = measured; "inferred" = a likely cause worked out from measurements
    confidence: str = "high"  # high | medium | low - how sure the finding is, given how much was measured

    def line(self) -> str:
        how = "measured" if self.basis == "observed" else f"inferred, {self.confidence} confidence"
        return f"[{self.severity}] {self.message} ({self.evidence}; {how}). {self.suggestion}"


BASELINE_MIN_SAMPLES = 200      # about three hours of readings before "normal for you" means anything
ABOVE_NORMAL_FACTOR = 1.25


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(q * len(ordered)), len(ordered) - 1)]


def baseline(rows: list[dict]) -> dict[str, dict[str, float]]:
    """What is normal on THIS PC: median and 90th percentile of CPU and memory over the kept history."""
    out: dict[str, dict[str, float]] = {}
    for key in ("cpu", "mem_percent"):
        values = _values(rows, key)
        if len(values) >= BASELINE_MIN_SAMPLES:
            out[key] = {"median": round(_percentile(values, 0.5), 1), "p90": round(_percentile(values, 0.9), 1),
                        "samples": len(values)}
    return out


def analyse_baseline(rows: list[dict], recent: int = 15) -> list[Finding]:
    """The last ``recent`` readings against this PC's own normal. An inference: 'unusual for you', not 'broken'."""
    base = baseline(rows[:-recent] if len(rows) > recent else [])
    findings = []
    for key, label in (("cpu", "CPU"), ("mem_percent", "memory")):
        if key not in base:
            continue
        tail = _values(rows[-recent:], key)
        if len(tail) < recent // 2:
            continue
        now = sum(tail) / len(tail)
        limit = max(base[key]["p90"] * ABOVE_NORMAL_FACTOR, 50.0)
        if now > limit:
            confidence = "high" if base[key]["samples"] >= 1000 else "medium"
            findings.append(Finding("above_normal", "info", f"{label} use is higher than is normal for this PC",
                                    f"{now:.0f}% over the last {len(tail)} minutes; usually at most {base[key]['p90']:.0f}%",
                                    "Something new is running or a program changed; check what is open.",
                                    basis="inferred", confidence=confidence))
    return findings


def _values(rows: list[dict], key: str) -> list[float]:
    return [r[key] for r in rows if r.get(key) is not None]


def analyse_history(rows: list[dict]) -> list[Finding]:
    findings: list[Finding] = []
    if len(rows) < 3:
        return findings
    cpu = [r["cpu"] for r in rows]
    streak = best = 0
    for value in cpu:
        streak = streak + 1 if value is not None and value >= HIGH_CPU else 0
        best = max(best, streak)
    if best >= HIGH_CPU_SAMPLES:
        findings.append(Finding("high_cpu", "warning", "The CPU stayed almost fully busy",
                                f"{best} minutes in a row at {HIGH_CPU:.0f}% or more",
                                "Ask 'what is using my CPU' to see which program; nothing was stopped."))
    mem = [(datetime.fromisoformat(r["ts"]), r["mem_percent"]) for r in rows if r["mem_percent"] is not None]
    if len(mem) >= 5:
        span_h = (mem[-1][0] - mem[0][0]).total_seconds() / 3600
        growth = (mem[-1][1] - mem[0][1]) / span_h if span_h >= 0.5 else 0.0
        if growth >= MEMORY_GROWTH_PER_HOUR and mem[-1][1] >= 60:
            findings.append(Finding("memory_growth", "warning", "Memory use keeps climbing",
                                    f"+{growth:.1f} points an hour, now {mem[-1][1]:.0f}%",
                                    "A program may be leaking memory; restarting the biggest one usually helps.",
                                    basis="inferred", confidence="medium" if len(mem) >= 30 else "low"))
        elif mem[-1][1] >= MEMORY_HIGH:
            findings.append(Finding("high_memory", "warning", "Memory is nearly full", f"{mem[-1][1]:.0f}% used",
                                    "Close programs you aren't using."))
    disk = _values(rows, "disk_percent")
    if disk and disk[-1] >= LOW_DISK_PERCENT:
        findings.append(Finding("low_disk", "problem", "The disk is almost full", f"{disk[-1]:.0f}% used",
                                "Say 'clean temp files' or move large files; nothing was deleted."))
    latest = rows[-1]
    if latest.get("battery") is not None and latest["battery"] <= LOW_BATTERY and not latest.get("plugged"):
        findings.append(Finding("low_battery", "warning", "The battery is low and not charging",
                                f"{latest['battery']:.0f}%", "Plug in the charger."))
    return findings


def analyse_workers(workers: dict | None) -> list[Finding]:
    """The automation worker: a stopped loop, runs that never finish, work being refused."""
    findings: list[Finding] = []
    if not workers:
        return findings
    if workers.get("loop_alive") is False:
        findings.append(Finding("worker_stopped", "problem", "The automation worker is not running",
                                "its loop has ended", "Restart Jarvis; scheduled tasks won't run until then."))
    for item in workers.get("stalled", []):
        findings.append(Finding("worker_stalled", "warning", "An automation has been running for a long time",
                                f"{item['seconds'] // 60} minutes", "Check what it is doing; say 'pause all automations' if needed."))
    if workers.get("refused_runs", 0):
        findings.append(Finding("worker_overloaded", "warning", "Some automations were refused because too many were running",
                                f"{workers['refused_runs']} refused (limit {workers.get('limit')})",
                                "Spread scheduled tasks across different times."))
    return findings


def analyse_state(database, now: datetime | None = None, model_health: dict | None = None,
                  workers: dict | None = None) -> list[Finding]:
    """What Jarvis itself knows: failed or stuck work, failing models and a stalled worker."""
    now = now or datetime.now(timezone.utc)
    findings: list[Finding] = analyse_workers(workers)
    try:
        with database.connect() as conn:
            failed = conn.execute("SELECT name, last_status FROM automation_tasks WHERE enabled = 1 AND last_status IS NOT NULL "
                                  "AND last_status NOT IN ('ok', 'skipped while closed') AND last_status NOT LIKE 'missed%' "
                                  "AND last_status NOT LIKE 'interrupted%' AND last_status != 'running'").fetchall()
            stuck = conn.execute("SELECT title, state, updated_at FROM missions WHERE state IN ('running', 'verifying', 'paused')"
                                 ).fetchall()
            crashes = conn.execute("SELECT COUNT(*) FROM agent_runs WHERE state IN ('failed', 'interrupted') AND started_at >= ?",
                                   (_iso(now - timedelta(days=1)),)).fetchone()[0]
    except Exception:
        return findings
    for name, status in failed:
        findings.append(Finding("failed_automation", "warning", f"The automation '{name}' failed on its last run",
                                str(status)[:120], f"Say 'run {name}' to try again, or 'show automation {name}'."))
    for title, state, updated in stuck:
        try:
            age = now - datetime.fromisoformat(updated)
        except (TypeError, ValueError):
            continue
        if age > timedelta(hours=2):
            findings.append(Finding("stuck_mission", "warning", f"The mission '{title}' has been {state} for a while",
                                    f"last change {int(age.total_seconds() // 3600)} h ago",
                                    "Say 'continue my mission' or 'cancel the mission'."))
    if crashes >= 3:
        findings.append(Finding("repeated_failures", "problem", "Several requests failed or were cut off today",
                                f"{crashes} in the last 24 hours", "Open Mission Control to see which ones and why."))
    for model, info in (model_health or {}).items():
        if info.get("skipped_for", 0) > 0:
            findings.append(Finding("model_failing", "warning", f"The model '{model}' keeps failing",
                                    f"{info['failures']} failures, last: {info['last_error'][:80]}",
                                    "Check that Ollama is running and the model is installed (`ollama list`)."))
        rate = info.get("tool_call_ok_rate")
        if rate is not None and info.get("tool_calls", 0) >= 5 and rate < 0.8:
            findings.append(Finding("model_tool_calls", "warning", f"The model '{model}' often calls tools wrongly",
                                    f"{round((1 - rate) * 100)}% of its last {info['tool_calls']} tool calls named a tool that "
                                    "doesn't exist or had unusable arguments (this session)",
                                    "Use a larger model for actions (AI_SMART_MODEL) or check `python run.py --eval-quality`."))
    return findings


def diagnose(database, now: datetime | None = None, model_health: dict | None = None, hours: float = 24,
             workers: dict | None = None) -> list[Finding]:
    store = MetricsStore(database)
    return (analyse_history(store.history(hours, now)) + analyse_baseline(store.history(24 * 14, now))
            + analyse_state(database, now, model_health, workers))


def summary(database, now: datetime | None = None, model_health: dict | None = None,
            workers: dict | None = None) -> tuple[str, dict[str, Any]]:
    """(plain-language text, data) for the 'system_health' tool: latest numbers, 24 h range, findings."""
    rows = MetricsStore(database).history(24, now)
    findings = diagnose(database, now, model_health, workers=workers)
    parts = []
    if rows:
        latest = rows[-1]

        def show(key: str, label: str, unit: str = "%") -> str:
            values = _values(rows, key)
            if not values:
                return f"{label}: not available on this PC"
            return f"{label} {values[-1]:.0f}{unit} now (24 h: {min(values):.0f}-{max(values):.0f}{unit})"

        parts = [show("cpu", "CPU"), show("mem_percent", "memory"), show("disk_percent", "disk")]
        if latest.get("battery") is not None:
            parts.append(f"battery {latest['battery']:.0f}% ({'charging' if latest.get('plugged') else 'on battery'})")
        parts.append(f"{len(rows)} readings kept")
    else:
        parts.append("no readings recorded yet (they are taken about once a minute while Jarvis runs)")
    text = "; ".join(parts) + "."
    text += (" Findings: " + " ".join(f.line() for f in findings[:5])) if findings else " No problems found."
    return text, {"readings": len(rows), "findings": [f.__dict__ for f in findings]}
