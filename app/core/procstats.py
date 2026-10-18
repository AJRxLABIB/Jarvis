"""Per-program baselines and crash correlation. Opt-in (``PROCESS_HISTORY``), off by default.

Only the program's executable name, CPU % and memory (MB) of the few busiest programs are stored - never command
lines, window titles, paths or contents. Findings are inferences ("uses more than it usually does", "was heavy just
before a failure") and are labelled with a confidence; nothing is ever stopped automatically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

TOP_N = 5
MIN_SAMPLES = 30            # readings of one program before "normal for it" means anything
ABOVE_FACTOR = 1.5
BEFORE_FAILURE = timedelta(minutes=10)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def sample_top(n: int = TOP_N) -> list[dict[str, Any]]:
    """The ``n`` busiest programs right now (by CPU, then memory). Names only."""
    import psutil

    rows = []
    for proc in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            mem = info.get("memory_info")
            if not info.get("name") or mem is None:
                continue
            rows.append({"name": str(info["name"])[:60], "cpu": float(info.get("cpu_percent") or 0.0),
                         "mem_mb": round(mem.rss / 1048576, 1)})
        except Exception:
            continue
    # one row per program name (many chrome.exe processes -> summed)
    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        m = merged.setdefault(r["name"].lower(), {"name": r["name"], "cpu": 0.0, "mem_mb": 0.0})
        m["cpu"] += r["cpu"]
        m["mem_mb"] += r["mem_mb"]
    ranked = sorted(merged.values(), key=lambda r: (r["cpu"], r["mem_mb"]), reverse=True)
    return [{**r, "cpu": round(r["cpu"], 1), "mem_mb": round(r["mem_mb"], 1)} for r in ranked[:n]]


class ProcessStore:
    def __init__(self, database, retention: timedelta = timedelta(days=14)) -> None:
        self._db = database
        self.retention = retention

    def record(self, samples: list[dict[str, Any]], now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        if not samples:
            return 0
        with self._db.transaction() as conn:
            conn.executemany("INSERT INTO process_samples (ts, name, cpu, mem_mb) VALUES (?, ?, ?, ?)",
                             [(_iso(now), s["name"], s["cpu"], s["mem_mb"]) for s in samples])
            conn.execute("DELETE FROM process_samples WHERE ts < ?", (_iso(now - self.retention),))
        return len(samples)

    def delete(self) -> int:
        with self._db.transaction() as conn:
            return conn.execute("DELETE FROM process_samples").rowcount

    def baselines(self) -> dict[str, dict[str, float]]:
        """Per program: median CPU and memory over everything kept (only programs with enough readings)."""
        with self._db.connect() as conn:
            rows = conn.execute("SELECT name, cpu, mem_mb FROM process_samples ORDER BY name").fetchall()
        grouped: dict[str, list[tuple[float, float]]] = {}
        for r in rows:
            grouped.setdefault(r["name"], []).append((r["cpu"], r["mem_mb"]))
        out = {}
        for name, values in grouped.items():
            if len(values) >= MIN_SAMPLES:
                cpus, mems = sorted(v[0] for v in values), sorted(v[1] for v in values)
                out[name] = {"cpu_median": cpus[len(cpus) // 2], "mem_median": mems[len(mems) // 2],
                             "samples": len(values)}
        return out

    def unusual(self, current: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Programs using clearly more memory than their own median (and a meaningful amount)."""
        base = self.baselines()
        found = []
        for s in current:
            b = base.get(s["name"])
            if b and s["mem_mb"] > max(b["mem_median"] * ABOVE_FACTOR, b["mem_median"] + 200):
                found.append({"name": s["name"], "mem_mb": s["mem_mb"], "usual_mem_mb": round(b["mem_median"], 1),
                              "basis": "inferred", "confidence": "high" if b["samples"] >= 300 else "medium"})
        return found

    def around_failures(self, days: int = 7, now: datetime | None = None) -> list[dict[str, Any]]:
        """For each failed or cut-off request, the programs that were heaviest in the 10 minutes before it.

        This is a correlation, not a cause: low confidence unless the same program shows up before several failures.
        """
        now = now or datetime.now(timezone.utc)
        with self._db.connect() as conn:
            fails = conn.execute("SELECT id, started_at FROM agent_runs WHERE state IN ('failed', 'interrupted') "
                                 "AND started_at >= ? ORDER BY started_at", (_iso(now - timedelta(days=days)),)).fetchall()
            per_failure = []
            for f in fails:
                try:
                    at = datetime.fromisoformat(f["started_at"])
                except (TypeError, ValueError):
                    continue
                heavy = conn.execute("SELECT name, MAX(mem_mb) AS mem, MAX(cpu) AS cpu FROM process_samples "
                                     "WHERE ts BETWEEN ? AND ? GROUP BY name ORDER BY mem DESC LIMIT 1",
                                     (_iso(at - BEFORE_FAILURE), _iso(at))).fetchone()
                if heavy:
                    per_failure.append({"run": f["id"], "at": f["started_at"], "program": heavy["name"],
                                        "mem_mb": heavy["mem"], "cpu": heavy["cpu"]})
        counts: dict[str, int] = {}
        for item in per_failure:
            counts[item["program"]] = counts.get(item["program"], 0) + 1
        for item in per_failure:
            item["times_before_failures"] = counts[item["program"]]
            item["basis"] = "inferred"
            item["confidence"] = "medium" if counts[item["program"]] >= 3 else "low"
        return per_failure


def describe(unusual: list[dict[str, Any]], failures: list[dict[str, Any]], have_data: bool) -> str:
    if not have_data:
        return ("No program history yet. It is off by default; turn on PROCESS_HISTORY to record the five busiest "
                "programs' names with CPU/memory numbers (nothing else).")
    parts = []
    for u in unusual:
        parts.append(f"{u['name']} is using {u['mem_mb']:.0f} MB; it usually uses {u['usual_mem_mb']:.0f} MB "
                     f"(inferred, {u['confidence']} confidence).")
    repeated = {f["program"]: f for f in failures if f["times_before_failures"] >= 2}
    for name, f in repeated.items():
        parts.append(f"{name} was the heaviest program just before {f['times_before_failures']} failed requests "
                     f"(a correlation, {f['confidence']} confidence - not proof it caused them).")
    return " ".join(parts) or "Programs look normal compared with their own history, and no program repeatedly preceded a failure."
