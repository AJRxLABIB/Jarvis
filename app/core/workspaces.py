"""Workspace objects: one named place that joins a project folder, the files you work on, a saved window layout, the goal and the
unfinished mission behind it, and notes - with an honest preview of what restoring it would do.

Stored in the settings store (``workspaces.items``): your own configuration, nothing observed or guessed. Nothing is started or moved
until you ask to restore, and restoring runs ordinary tool calls (open_path, arrange_windows) through the normal executor, so every
permission, confirmation and audit rule still applies. The preview reports what is missing (folder, files, apps of the layout) BEFORE
anything is opened. A signal can be switched off per workspace (``ignore``: layout, goal, mission, files) and switching one off is
just that: it is not learned as a preference about you.

Limits: layout restore needs Windows and the apps installed; a layout restores windows it can match, and starts missing apps only
when their saved path still exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.memory.settings_store import SettingsStore
from app.tools.base import ToolError

KEY = "workspaces.items"
MAX_WORKSPACES = 30
MAX_FILES = 12
SIGNALS = ("files", "layout", "goal", "mission")
_NAME = re.compile(r"^[\w][\w .'-]{0,39}$", re.UNICODE)


def key_of(name: str) -> str:
    text = " ".join((name or "").lower().split())
    if not text or not _NAME.match(text):
        raise ToolError("Give the workspace a short name (letters, digits, spaces).", "invalid_name")
    return text


def load(db: Any) -> dict[str, dict[str, Any]]:
    data = SettingsStore(db).get(KEY, {})
    return data if isinstance(data, dict) else {}


def _store(db: Any, items: dict[str, dict[str, Any]]) -> None:
    SettingsStore(db).set(KEY, items)


def save(ctx: Any, name: str, *, folder: str = "", files: list[str] | None = None, layout: str = "", goal: str = "",
         notes: str = "") -> dict[str, Any]:
    """Create or update a workspace. Only the fields given are changed on an existing one."""
    db = ctx.database
    items = load(db)
    key = key_of(name)
    if key not in items and len(items) >= MAX_WORKSPACES:
        raise ToolError(f"At most {MAX_WORKSPACES} workspaces; delete one first.", "too_many")
    entry = dict(items.get(key) or {"name": name.strip(), "folder": "", "files": [], "layout": "", "goal_id": None, "notes": "", "ignore": []})
    policy = ctx.get_path_policy()
    if folder.strip():
        path = Path(policy.resolve(folder))
        if not path.is_dir():
            raise ToolError(f"{folder} is not a folder that exists.", "not_found")
        entry["folder"] = str(path)
    if files is not None:
        if len(files) > MAX_FILES:
            raise ToolError(f"At most {MAX_FILES} files per workspace.", "too_many")
        entry["files"] = [str(Path(policy.resolve(f))) for f in files if f.strip()]
    if layout.strip():
        from app.tools.layouts import layout_key, load_layouts

        lk = layout_key(layout)
        if lk not in load_layouts(ctx):
            raise ToolError(f"There's no saved layout called '{lk}'.", "not_found")
        entry["layout"] = lk
    if goal.strip():
        from app.core.goals import GoalStore

        wanted = goal.strip().lower()
        match = next((g for g in GoalStore(db).active() if wanted == str(g.id) or wanted in g.title.lower()), None)
        if match is None:
            raise ToolError(f"No active goal matches '{goal}'.", "not_found")
        entry["goal_id"] = match.id
    if notes.strip():
        entry["notes"] = notes.strip()[:500]
    if not any(entry.get(k) for k in ("folder", "files", "layout", "goal_id")):
        raise ToolError("A workspace needs at least a folder, files, a layout or a goal.", "empty_workspace")
    items[key] = entry
    _store(db, items)
    return entry


def delete(db: Any, name: str) -> bool:
    items = load(db)
    if items.pop(key_of(name), None) is None:
        return False
    _store(db, items)
    return True


def set_ignored(db: Any, name: str, signal: str, ignored: bool) -> list[str]:
    if signal not in SIGNALS:
        raise ToolError(f"Signal must be one of: {', '.join(SIGNALS)}.", "invalid_signal")
    items = load(db)
    entry = items.get(key_of(name))
    if entry is None:
        raise ToolError(f"There's no workspace called '{name}'.", "not_found")
    now = set(entry.get("ignore", [])) | {signal} if ignored else set(entry.get("ignore", [])) - {signal}
    entry["ignore"] = sorted(now)
    _store(db, items)
    return entry["ignore"]


@dataclass
class Item:
    signal: str
    label: str
    status: str            # ok | missing | skipped | info
    detail: str = ""


@dataclass
class Preview:
    name: str
    items: list[Item] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)      # tool calls restore would make, in order
    notes: str = ""

    @property
    def missing(self) -> list[Item]:
        return [i for i in self.items if i.status == "missing"]

    def describe(self) -> str:
        lines = [f"Workspace '{self.name}' - what restoring would do (nothing has been opened yet):"]
        for i in self.items:
            lines.append(f"  [{i.status.upper()}] {i.signal}: {i.label}" + (f" - {i.detail}" if i.detail else ""))
        lines.append("Steps: " + (" -> ".join(f"{s['tool']}({', '.join(str(v) for v in s['arguments'].values())})" for s in self.steps)
                                  if self.steps else "none"))
        if self.missing:
            lines.append(f"{len(self.missing)} thing(s) are missing; those steps are left out.")
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        return "\n".join(lines)


def preview(ctx: Any, name: str) -> Preview:
    db = ctx.database
    entry = load(db).get(key_of(name))
    if entry is None:
        known = sorted(load(db))
        raise ToolError(f"There's no workspace called '{name}'." + (f" Yours: {', '.join(known)}." if known else ""), "not_found")
    ignore = set(entry.get("ignore", []))
    out = Preview(entry.get("name", name), notes=entry.get("notes", ""))
    folder = entry.get("folder", "")
    if folder:
        if Path(folder).is_dir():
            out.items.append(Item("folder", folder, "ok"))
            out.steps.append({"tool": "open_path", "arguments": {"path": folder}})
        else:
            out.items.append(Item("folder", folder, "missing", "the folder no longer exists"))
    if "files" in ignore:
        out.items.append(Item("files", f"{len(entry.get('files', []))} file(s)", "skipped", "switched off for this workspace"))
    for f in ([] if "files" in ignore else entry.get("files", [])):
        if Path(f).exists():
            out.items.append(Item("files", f, "ok"))
            out.steps.append({"tool": "open_path", "arguments": {"path": f}})
        else:
            out.items.append(Item("files", f, "missing", "moved or deleted"))
    layout = entry.get("layout", "")
    if layout and "layout" in ignore:
        out.items.append(Item("layout", layout, "skipped", "switched off for this workspace"))
    elif layout:
        from app.tools.layouts import load_layouts

        saved = load_layouts(ctx).get(layout)
        if not saved:
            out.items.append(Item("layout", layout, "missing", "the saved layout was deleted"))
        else:
            windows = saved.get("windows", [])
            absent = [Path(w.get("path") or w.get("process", "?")).name for w in windows
                      if w.get("path") and not Path(w["path"]).is_file()]
            detail = f"{len(windows)} window(s)" + (f"; apps that can no longer be started: {', '.join(sorted(set(absent)))}" if absent else "")
            out.items.append(Item("layout", layout, "ok", detail))
            out.steps.append({"tool": "arrange_windows", "arguments": {"action": "restore", "layout": layout}})
    gid = entry.get("goal_id")
    if gid and "goal" in ignore:
        out.items.append(Item("goal", f"#{gid}", "skipped", "switched off for this workspace"))
    elif gid:
        from app.core.goals import GoalStore

        try:
            g = GoalStore(db).get(int(gid))
            out.items.append(Item("goal", g.title, "ok" if g.status == "active" else "info", f"{g.status}, {g.progress}% done"))
        except Exception:
            out.items.append(Item("goal", f"#{gid}", "missing", "the goal no longer exists"))
        if "mission" not in ignore:
            from app.core.missions import MissionStore

            for m in MissionStore(db).recent(20, states=("paused", "running", "interrupted", "needs_attention")):
                if m.goal_id == gid:
                    out.items.append(Item("mission", m.title, "info", f"{m.state}, steps {m.progress()} - resume it yourself; restoring does not"))
                    break
    return out


async def restore(ctx: Any, name: str) -> tuple[Preview, list[tuple[str, bool, str]]]:
    """Run the preview's steps through the normal executor. Returns (preview, [(tool, ok, message)])."""
    from app.ai.base import ToolCall

    pv = preview(ctx, name)
    if ctx.executor is None:
        raise ToolError("Restoring needs the tool executor.", "no_executor")
    results = []
    for step in pv.steps:
        execution = await ctx.executor.execute(ToolCall(step["tool"], step["arguments"]))
        results.append((step["tool"], execution.result.ok, execution.result.message[:160]))
    return pv, results
