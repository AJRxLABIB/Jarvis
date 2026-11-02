"""What changed while I was away: one honest comparison between a moment in the past and now.

Sources (each optional - one that cannot be read is listed under "could not check", never silently skipped or invented):
events (the bus log), notifications (grouped with the notification classifier), missions (state / updated since), watched folders (file names and
times only - never contents), devices (trust / connection now vs the last look), guardian incidents (opened since).

"Away" is the time you give ("since 3 hours ago", "since yesterday") or the stored last-active marker (``mark_active``, written when you talk to
Jarvis or call this tool). The file scan is bounded (folder count, files, depth) and read-only. Pure over injected readers; the real readers use
the database and folders. Nothing is changed by looking.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

KEY_ACTIVE = "away_marker"
KEY_DEVICES = "away_devices"
MAX_FILES = 4000
MAX_DEPTH = 4
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "$RECYCLE.BIN", ".cache", ".gradle"}


@dataclass
class Readers:
    events: Callable[[datetime], list[dict[str, Any]]] = lambda since: []
    notifications: Callable[[datetime], list[dict[str, Any]]] = lambda since: []
    missions: Callable[[datetime], list[dict[str, Any]]] = lambda since: []
    files: Callable[[datetime], tuple[list[tuple[str, str]], bool]] = lambda since: ([], False)      # ([(kind, path)], truncated)
    devices: Callable[[], dict[str, str]] = lambda: {}                                              # key -> "name: connection"
    previous_devices: Callable[[], dict[str, str]] = lambda: {}
    incidents: Callable[[datetime], list[str]] = lambda since: []


@dataclass
class Report:
    since: datetime
    now: datetime
    sections: dict[str, list[str]] = field(default_factory=dict)
    unchecked: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not any(self.sections.values())

    def text(self) -> str:
        span = self.now - self.since
        hours = span.total_seconds() / 3600
        head = f"Since {self.since.astimezone().strftime('%a %H:%M')} ({hours:.1f} h ago):" if hours < 48 else f"Since {self.since.astimezone():%d %b %H:%M}:"
        if self.empty:
            body = ["Nothing worth reporting changed in what I can see."]
        else:
            body = []
            for title, items in self.sections.items():
                if items:
                    body.append(f"{title}:")
                    body += [f"  - {i}" for i in items[:8]] + ([f"  - ... and {len(items) - 8} more"] if len(items) > 8 else [])
        if self.truncated:
            body.append("The file scan stopped at its limit, so there may be more file changes.")
        if self.unchecked:
            body.append("Could not check: " + ", ".join(self.unchecked) + ".")
        return "\n".join([head, *body])


def parse_since(text: str, now: datetime, marker: datetime | None) -> datetime | None:
    """"3 hours", "90 minutes", "2 days", "yesterday", "" -> marker (or None when there is none). Never guesses."""
    t = (text or "").strip().lower().removeprefix("since").strip().removesuffix("ago").strip()
    if not t:
        return marker
    if t in ("yesterday", "last night"):
        return now - timedelta(hours=24)
    if t in ("this morning", "morning"):
        return now.replace(hour=6, minute=0, second=0, microsecond=0) if now.hour >= 6 else now - timedelta(hours=24)
    parts = t.replace("a ", "1 ").replace("an ", "1 ").split()
    if len(parts) >= 2:
        try:
            n = float(parts[0])
        except ValueError:
            return None
        unit = parts[1]
        for names, seconds in ((("minute", "min", "minutes", "mins"), 60), (("hour", "hours", "hr", "hrs", "h"), 3600), (("day", "days", "d"), 86400),
                               (("week", "weeks", "w"), 604800)):
            if unit in names:
                return now - timedelta(seconds=n * seconds)
    return None


def compare(readers: Readers, since: datetime, now: datetime | None = None) -> Report:
    now = now or datetime.now(timezone.utc)
    rep = Report(since, now)

    def section(name: str, label: str, build: Callable[[], list[str]]) -> None:
        try:
            rep.sections[label] = build()
        except Exception:
            rep.unchecked.append(name)

    def events() -> list[str]:
        from app.core import event_intel

        rows = readers.events(since)
        intel = [event_intel.normalise(r) for r in rows]
        out = []
        for g in event_intel.correlate(intel, now=since):
            d = event_intel.diagnose(g)
            if d.priority in ("high", "urgent") or len(g) > 1:
                out.append(f"{d.title} ({len(g)} event{'s' if len(g) > 1 else ''})")
        return out

    def notifications() -> list[str]:
        from app.core import notification_intel

        rows = readers.notifications(since)
        if not rows:
            return []
        results = notification_intel.classify_stored(rows)
        counts: dict[str, int] = {}
        for r in results:
            counts[r.label] = counts.get(r.label, 0) + 1
        important = [f"{n} {label}" for label, n in counts.items() if label in ("critical", "suspicious", "action_required", "important")]
        return ([", ".join(important)] if important else []) + ([f"{len(rows)} notifications in total"] if len(rows) > 5 else [])

    def missions() -> list[str]:
        return [f"{m['title']}: {m['state']}" for m in readers.missions(since)]

    def files() -> list[str]:
        items, rep.truncated = readers.files(since)
        by: dict[str, list[str]] = {}
        for kind, path in items:
            by.setdefault(kind, []).append(path)
        return [f"{kind}: {len(paths)} (e.g. {', '.join(Path(p).name for p in paths[:3])})" for kind, paths in by.items()]

    def devices() -> list[str]:
        now_d, before = readers.devices(), readers.previous_devices()
        out = []
        for key, state in now_d.items():
            old = before.get(key)
            if old is None and before:
                out.append(f"new: {state}")
            elif old is not None and old != state:
                out.append(f"{state} (was: {old.split(': ', 1)[-1]})")
        out += [f"gone: {state}" for key, state in before.items() if key not in now_d]
        return out

    section("events", "Events and problems", events)
    section("notifications", "Notifications", notifications)
    section("missions", "Missions", missions)
    section("files", "Files in watched folders", files)
    section("devices", "Devices", devices)
    section("guardian", "Security", lambda: readers.incidents(since))
    return rep


# ---- real readers ----------------------------------------------------------------------------------------------------------------------

def _iso(since: datetime) -> str:
    return since.astimezone(timezone.utc).isoformat(timespec="seconds")


def scan_folders(folders: list[str], since: datetime, *, max_files: int = MAX_FILES, max_depth: int = MAX_DEPTH,
                 walker: Callable[[str], Any] = os.walk) -> tuple[list[tuple[str, str]], bool]:
    """Files created / modified since ``since`` (names only). Bounded; hidden system folders skipped; never opens a file."""
    cutoff = since.timestamp()
    found: list[tuple[str, str]] = []
    seen = 0
    for root in folders:
        base_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in walker(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            if dirpath.count(os.sep) - base_depth >= max_depth:
                dirnames[:] = []
            for name in filenames:
                seen += 1
                if seen > max_files:
                    return found, True
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_mtime >= cutoff:
                    kind = "created" if st.st_ctime >= cutoff and abs(st.st_ctime - st.st_mtime) < 2 else "modified"
                    found.append((kind, path))
    return found, False


def real_readers(db: Any, folders: list[str], store: Any = None) -> Readers:
    def events(since: datetime) -> list[dict[str, Any]]:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM events WHERE ts >= ? ORDER BY id", (_iso(since),))]

    def notifications(since: datetime) -> list[dict[str, Any]]:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM notifications WHERE ts >= ? ORDER BY id", (_iso(since),))]

    def missions(since: datetime) -> list[dict[str, Any]]:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT title, state FROM missions WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT 20",
                                                  (_iso(since),))]

    def devices() -> dict[str, str]:
        from app.core import device_mesh

        return {n.key: f"{n.name}: {n.connection}" for n in device_mesh.snapshot(db)}

    def incidents(since: datetime) -> list[str]:
        from app.memory.settings_store import SettingsStore

        raw = SettingsStore(db).get("guardian_incidents", []) or []
        return [f"{i.get('title', 'incident')} ({i.get('severity', '?')})" for i in raw if str(i.get("ts", "")) >= _iso(since)]

    return Readers(events=events, notifications=notifications, missions=missions, files=lambda s: scan_folders(folders, s), devices=devices,
                   previous_devices=lambda: (store.get(KEY_DEVICES, {}) if store is not None else {}) or {}, incidents=incidents)


def mark_active(store: Any, now: datetime | None = None, devices: dict[str, str] | None = None) -> None:
    store.set(KEY_ACTIVE, (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
    if devices is not None:
        store.set(KEY_DEVICES, devices)


def marker(store: Any) -> datetime | None:
    raw = store.get(KEY_ACTIVE)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None
