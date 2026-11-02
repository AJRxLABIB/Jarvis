"""The digital twin and what-if preview.

TWIN: the last thing Jarvis OBSERVED about your environment (devices, missions, disk space, memory, files it looked at), each with
its source and when it was seen. Nothing is guessed and nothing is watched in the background: observations are made only when
you (or a mission) ask for a refresh or a what-if check, from stores Jarvis already keeps and from cheap local reads (free disk
space, whether a path exists). An entry older than its kind's lifetime is shown as STALE, and a thing never observed is UNKNOWN -
the twin never presents an old reading as current.

WHAT-IF: before a plan runs, ``what_if`` looks at each step WITHOUT running anything: its validated arguments, risk level, which
paths/devices it would touch (looked up now), name collisions, whether Jarvis has an automatic undo for it, whether it needs the
internet, plausible failure modes, and a safer alternative where one exists. It is an ESTIMATE from tool metadata and a few reads,
not a simulation of the application itself and not a guarantee; what a tool really does is only known when it runs.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.security.permissions import RiskLevel

# how long an observation counts as current, by kind (seconds)
LIFETIME = {"device": 300, "mission": 120, "disk": 600, "memory": 120, "path": 600}
DEFAULT_LIFETIME = 300
PATH_KEYS = ("path", "source", "destination", "folder", "directory", "file", "target", "output", "dest")
# tools Jarvis can automatically undo (app/core/undo.py::inverse_of) - everything else that changes things has no automatic undo
UNDOABLE = {"move_item", "rename_item", "batch_rename", "edit_code", "zip_items", "extract_zip", "merge_pdfs", "convert_image",
            "word_to_pdf", "create_item", "volume_control", "brightness_control", "device_toggle"}
RECOVERABLE = {"delete_item"}          # goes to the Recycle Bin: restorable by hand, not by "undo"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ observations


@dataclass
class Observation:
    kind: str
    key: str
    value: Any
    source: str
    observed_at: str
    age_s: float = 0.0
    state: str = "fresh"                       # fresh | stale

    def line(self) -> str:
        shown = self.value if isinstance(self.value, str) else json.dumps(self.value, ensure_ascii=False, default=str)
        return f"[{self.state.upper()}] {self.kind} {self.key}: {shown[:120]} (from {self.source}, {_ago(self.age_s)} ago)"


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    return f"{seconds / 3600:.1f} h"


def observe(db: Any, kind: str, key: str, value: Any, source: str, at: datetime | None = None) -> None:
    stamp = (at or _now()).astimezone(timezone.utc).isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.execute("INSERT INTO twin_observations (kind, key, value, source, observed_at) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT(kind, key) DO UPDATE SET value=excluded.value, source=excluded.source, observed_at=excluded.observed_at",
                     (kind, key[:200], json.dumps(value, ensure_ascii=False, default=str)[:2000], source[:80], stamp))


def snapshot(db: Any, now: datetime | None = None, kind: str | None = None) -> list[Observation]:
    now = (now or _now()).astimezone(timezone.utc)
    with db.connect() as conn:
        rows = conn.execute("SELECT kind, key, value, source, observed_at FROM twin_observations"
                            + (" WHERE kind = ?" if kind else "") + " ORDER BY kind, key", (kind,) if kind else ()).fetchall()
    out = []
    for k, key, value, source, at in rows:
        try:
            age = (now - datetime.fromisoformat(at)).total_seconds()
            val = json.loads(value)
        except ValueError:
            continue
        out.append(Observation(k, key, val, source, at, age, "stale" if age > LIFETIME.get(k, DEFAULT_LIFETIME) else "fresh"))
    return out


def forget(db: Any, kind: str | None = None) -> int:
    with db.transaction() as conn:
        cur = conn.execute("DELETE FROM twin_observations" + (" WHERE kind = ?" if kind else ""), (kind,) if kind else ())
        return cur.rowcount


def refresh(ctx: Any) -> list[str]:
    """Observe what Jarvis can see cheaply and locally. Returns what was read (and what could not be)."""
    db, notes = ctx.database, []
    try:
        from app.core.devices import DeviceRegistry

        for d in DeviceRegistry(db).all():
            observe(db, "device", d.name, {"state": d.last_state, "last_seen": d.last_seen, "self": d.is_self}, "device registry")
        notes.append("devices")
    except Exception as exc:  # a missing registry must not stop the rest
        notes.append(f"devices: not read ({type(exc).__name__})")
    try:
        from app.core.missions import MissionStore

        for m in MissionStore(db).recent(10):
            observe(db, "mission", m.id, {"title": m.title, "state": m.state, "progress": m.progress()}, "mission store")
        notes.append("missions")
    except Exception as exc:
        notes.append(f"missions: not read ({type(exc).__name__})")
    try:
        data_dir = ctx.paths.data_dir if getattr(ctx, "paths", None) is not None else Path.home()
        usage = shutil.disk_usage(data_dir)
        observe(db, "disk", str(data_dir)[:80], {"free_gb": round(usage.free / 1e9, 1), "total_gb": round(usage.total / 1e9, 1)}, "disk usage")
        notes.append("disk")
    except Exception as exc:
        notes.append(f"disk: not read ({type(exc).__name__})")
    try:
        import psutil

        mem = psutil.virtual_memory()
        observe(db, "memory", "ram", {"available_gb": round(mem.available / 1e9, 1), "percent": mem.percent}, "psutil")
        notes.append("memory")
    except Exception:
        notes.append("memory: not read (psutil unavailable)")
    return notes


def describe(db: Any, now: datetime | None = None) -> str:
    obs = snapshot(db, now)
    if not obs:
        return "The twin is empty: nothing has been observed yet (say 'refresh the twin'). Unobserved things are UNKNOWN, not 'fine'."
    stale = sum(o.state == "stale" for o in obs)
    head = f"{len(obs)} observations, {stale} stale (older than their lifetime - re-observe before relying on them)."
    return head + "\n" + "\n".join(o.line() for o in obs[:40])


# ------------------------------------------------------------------ what-if


@dataclass
class StepImpact:
    number: int
    tool: str
    level: int = 0
    changes_things: bool = False
    touches: list[str] = field(default_factory=list)
    reversibility: str = ""
    problems: list[str] = field(default_factory=list)       # things that would stop or endanger the step
    failure_modes: list[str] = field(default_factory=list)
    safer: str = ""


@dataclass
class WhatIf:
    steps: list[StepImpact]
    highest: int = 0
    needs_approval: bool = False
    stale_dependencies: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(s.problems for s in self.steps)

    def describe(self) -> str:
        lines = ["WHAT-IF (an estimate from tool metadata and reads made just now - nothing was run, and it is not a guarantee):"]
        for s in self.steps:
            head = f"{s.number}. {s.tool} - risk level {s.level}, {'changes things' if s.changes_things else 'read-only'}"
            lines.append(head + (f", touches {', '.join(s.touches)}" if s.touches else "") + (f"; {s.reversibility}" if s.reversibility else ""))
            lines += [f"   PROBLEM: {p}" for p in s.problems]
            lines += [f"   may fail: {f}" for f in s.failure_modes[:3]]
            if s.safer:
                lines.append(f"   safer: {s.safer}")
        lines.append(f"Highest risk level {self.highest}; {'asks you before running' if self.needs_approval else 'no approval needed by risk level'}; "
                     f"{'BLOCKED until the problems above are fixed' if self.blocked else 'no blocking problem found'}.")
        if self.stale_dependencies:
            lines.append("Not recently observed (re-check before relying on it): " + "; ".join(self.stale_dependencies))
        return "\n".join(lines)


def _paths(arguments: dict[str, Any]) -> list[tuple[str, str]]:
    found = []
    for key, value in arguments.items():
        if isinstance(value, str) and any(w in key.lower() for w in PATH_KEYS) and value.strip():
            found.append((key, value))
        elif isinstance(value, list) and any(w in key.lower() for w in PATH_KEYS):
            found += [(key, v) for v in value if isinstance(v, str)]
    return found


def _resolve(raw: str, ctx: Any) -> Path:
    """The path as the tools will read it (relative names mean Desktop, Documents...), falling back to the plain path."""
    try:
        return Path(ctx.get_path_policy().resolve(raw))
    except Exception:
        return Path(raw).expanduser()


def _analyse(number: int, tool_name: str, arguments: dict[str, Any], ctx: Any, registry: Any, db: Any,
             will_create: set[str]) -> StepImpact:
    from app.security.autonomy import is_read_only
    from app.tools.base import ToolError

    impact = StepImpact(number, tool_name)
    tool = registry.get(tool_name)
    if tool is None:
        impact.problems.append(f"there is no tool named '{tool_name}'")
        return impact
    try:
        args = tool.args_model.model_validate(arguments)
    except Exception as exc:
        errors = getattr(exc, "errors", None)
        first = errors()[0] if callable(errors) else {"loc": (), "msg": str(exc)}
        impact.problems.append(f"invalid arguments ({'.'.join(str(p) for p in first['loc']) or 'arguments'}: {first['msg']})")
        return impact
    arguments = args.model_dump(exclude_defaults=True)
    try:
        level = tool.level_for(args, ctx)
    except ToolError:
        level = tool.level
    impact.level = int(level)
    impact.changes_things = not is_read_only(tool_name, arguments) and (int(level) > int(RiskLevel.SAFE) or tool_name in UNDOABLE | RECOVERABLE)
    resolved = {raw: _resolve(raw, ctx) for _, raw in _paths(arguments)}
    if tool.precheck is not None:
        try:
            tool.precheck(args, ctx)
        except ToolError as exc:
            if not any(str(p) in will_create for p in resolved.values()):    # an earlier step is going to create it
                impact.problems.append(f"the tool's own pre-check would refuse it now: {exc}")
        except Exception:
            pass
    for key, raw in _paths(arguments):
        path = resolved[raw]
        impact.touches.append(raw[:80])
        exists = path.exists() or str(path) in will_create
        observe(db, "path", raw[:150], {"exists": path.exists(), "is_dir": path.is_dir(), "symlink": path.is_symlink()}, "what-if check")
        if path.is_symlink() or Path(raw).expanduser().is_symlink():
            impact.problems.append(f"{raw} is a symbolic link / reparse point (Jarvis will not follow it silently)")
        is_target = key.lower() in ("destination", "dest", "output", "target") or tool_name in ("create_item", "zip_items")
        if tool_name == "create_item" and key == "path":
            is_target = True
        if is_target and exists and tool_name not in ("move_item",):
            impact.failure_modes.append(f"{raw} already exists (name collision: it could be overwritten or refused)")
        elif is_target and tool_name == "move_item" and path.is_file():
            impact.failure_modes.append(f"{raw} is an existing file (a move needs a folder)")
        if not is_target and not exists and impact.changes_things:
            impact.problems.append(f"{raw} does not exist (and no earlier step creates it)")
        elif not is_target and not exists:
            impact.failure_modes.append(f"{raw} does not exist")
        if is_target and tool_name in ("create_item", "zip_items", "move_item", "extract_zip", "convert_image", "merge_pdfs"):
            will_create.add(str(path))
    if tool.needs_network:
        offline = getattr(getattr(ctx, "settings", None), "offline_mode", "auto") == "on"
        if offline:
            impact.problems.append("it needs the internet and offline mode is on")
        else:
            impact.failure_modes.append("no internet connection (it needs one)")
    if impact.changes_things:
        if tool_name in UNDOABLE:
            impact.reversibility = "Jarvis can undo this automatically (say 'undo that')"
        elif tool_name in RECOVERABLE:
            impact.reversibility = "recoverable from the Recycle Bin by hand; no automatic undo"
        else:
            impact.reversibility = "NO automatic undo - treat as irreversible"
            impact.safer = "run it last, after the reversible steps, and check the earlier results first"
        if impact.level >= int(RiskLevel.DANGEROUS):
            impact.failure_modes.append("you decline the approval, which stops the plan at this step")
    if tool_name == "batch_rename" and arguments.get("apply"):
        impact.safer = "run it once with apply=false to preview the new names first"
    return impact


def what_if(steps: list[dict[str, Any]], ctx: Any, registry: Any | None = None, now: datetime | None = None) -> WhatIf:
    """Estimate what a plan would touch and risk, without running anything. ``steps`` = [{"tool", "arguments"}]."""
    registry = registry or ctx.get_tool_registry()
    will_create: set[str] = set()
    impacts, stale = [], []
    for i, raw in enumerate(steps, start=1):
        tool_name = str(raw.get("tool") or "")
        arguments = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
        impacts.append(_analyse(i, tool_name, arguments, ctx, registry, ctx.database, will_create))
    highest = max((s.level for s in impacts), default=0)
    for d in snapshot(ctx.database, now, "device"):
        if d.state == "stale" and any(d.key.lower() in json.dumps(s.get("arguments", {})).lower() for s in steps):
            stale.append(f"device {d.key} (last observed {_ago(d.age_s)} ago)")
    return WhatIf(impacts, highest, highest >= int(RiskLevel.MODERATE), stale)
