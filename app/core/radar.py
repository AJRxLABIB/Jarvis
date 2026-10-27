"""The problem radar's additions to the proactive engine (``app/core/proactive.py`` already scores, rate-limits, honours
quiet hours and categories, and de-duplicates for six hours - none of that is repeated here).

* two new LOCAL, read-only signal sources: resource pressure (free RAM, free disk on the data drive) and a paired phone
  that has stopped checking in. They read numbers; they do not look at the screen, files, messages or microphone.
* statistical anomalies (app/core/anomaly.py): tool slowdowns and failure spikes, PC load, unusual spending.
* feedback you give about an alert: snooze, mute, "useful", "not useful". Three net "not useful" ratings mute that alert
  for a week (reversible, and shown), so a false alarm stops recurring without anyone editing settings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.proactive import Candidate

AUTO_MUTE_AFTER = 3
AUTO_MUTE_DAYS = 7
LOW_RAM_GB, LOW_RAM_PCT = 1.0, 8.0
LOW_DISK_GB, LOW_DISK_PCT = 3.0, 5.0
PHONE_SILENT_HOURS = 24


# ------------------------------------------------------------------ signals


def resource_signals(ctx: Any) -> list[Candidate]:
    out = []
    try:
        import psutil

        mem = psutil.virtual_memory()
        free_gb, pct = mem.available / 1e9, mem.available * 100 / mem.total
        if free_gb < LOW_RAM_GB or pct < LOW_RAM_PCT:
            out.append(Candidate("resource:ram", "system", "Memory is running low",
                                 f"Only {free_gb:.1f} GB of RAM is free ({pct:.0f}%). Local models may slow down or fail to load.",
                                 0.6, 0.6, confidence=1.0, interruption=0.3))
        folder = getattr(getattr(ctx, "paths", None), "data_dir", None) or "."
        disk = psutil.disk_usage(str(folder))
        free_gb, pct = disk.free / 1e9, disk.free * 100 / disk.total
        if free_gb < LOW_DISK_GB or pct < LOW_DISK_PCT:
            out.append(Candidate("resource:disk", "system", "Disk space is running low",
                                 f"Only {free_gb:.1f} GB is free on the drive Jarvis stores its data on ({pct:.0f}%).",
                                 0.5, 0.7, confidence=1.0, interruption=0.3))
    except Exception:
        pass
    return out


def device_signals(ctx: Any, now: datetime | None = None) -> list[Candidate]:
    out = []
    now = now or datetime.now(timezone.utc)
    try:
        from app.remote.mobile import MobileRegistry

        for device in MobileRegistry(ctx.database).all():
            if device.trust != "trusted" or not device.last_seen:
                continue
            seen = datetime.fromisoformat(device.last_seen.replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            hours = (now - seen).total_seconds() / 3600
            if hours >= PHONE_SILENT_HOURS:
                out.append(Candidate(f"device:{device.id}:silent", "system", "A paired phone hasn't checked in",
                                     f"{device.name} was last seen {int(hours)} hours ago (it may be off, out of range, or "
                                     "the app was stopped).", 0.3, 0.4, confidence=0.8, interruption=0.2))
    except Exception:
        pass
    return out


def signals(ctx: Any, now: datetime | None = None) -> list[Candidate]:
    from app.core import anomaly

    found = resource_signals(ctx) + device_signals(ctx, now)
    try:
        found += anomaly.signals(ctx, now)           # "unusual for you" - statistics over your own history
    except Exception:
        pass
    return found


# ------------------------------------------------------------------ feedback


class Feedback:
    def __init__(self, database: Any, clock=lambda: datetime.now(timezone.utc)) -> None:
        self._db, self._clock = database, clock

    def _now(self) -> datetime:
        return self._clock()

    def _upsert(self, key: str, **fields: Any) -> None:
        stamp = self._now().isoformat(timespec="seconds")
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO radar_feedback (key, updated_at) VALUES (?, ?) ON CONFLICT (key) DO NOTHING", (key, stamp))
            for column, value in fields.items():
                conn.execute(f"UPDATE radar_feedback SET {column} = ?, updated_at = ? WHERE key = ?", (value, stamp, key))

    def row(self, key: str) -> dict[str, Any]:
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM radar_feedback WHERE key = ?", (key,)).fetchone()
        return dict(r) if r else {"key": key, "muted": 0, "snoozed_until": None, "useful": 0, "not_useful": 0}

    def mute(self, key: str) -> None:
        self._upsert(key, muted=1)

    def unmute(self, key: str) -> None:
        self._upsert(key, muted=0, snoozed_until=None, not_useful=0)

    def snooze(self, key: str, hours: float = 24) -> None:
        self._upsert(key, snoozed_until=(self._now() + timedelta(hours=hours)).isoformat(timespec="seconds"))

    def rate(self, key: str, useful: bool) -> str:
        row = self.row(key)
        useful_n, not_n = row["useful"] + int(useful), row["not_useful"] + int(not useful)
        self._upsert(key, useful=useful_n, not_useful=not_n)
        if not_n - useful_n >= AUTO_MUTE_AFTER:
            self.snooze(key, AUTO_MUTE_DAYS * 24)
            return f"Rated not useful {not_n} times: I won't raise this for {AUTO_MUTE_DAYS} days (unmute to undo)."
        return "Thanks - noted."

    def blocked(self, key: str) -> str:
        """Why this alert is held back ('' = not held back)."""
        row = self.row(key)
        if row["muted"]:
            return "you muted it"
        until = row["snoozed_until"]
        if until:
            try:
                if datetime.fromisoformat(until) > self._now():
                    return f"snoozed until {until[:16].replace('T', ' ')}"
            except ValueError:
                pass
        return ""

    def filter(self, candidates: list[Candidate]) -> tuple[list[Candidate], list[tuple[Candidate, str]]]:
        kept, held = [], []
        for c in candidates:
            why = self.blocked(c.key)
            (held.append((c, why)) if why else kept.append(c))
        return kept, held
