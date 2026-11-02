"""Work-state snapshots and honest resume.

A snapshot is a versioned note of a working session: project folder, the files and windows involved, the mission and its last confirmed
step, and open questions. It is NOT proof the machine still looks like that, so ``compare`` checks the snapshot against the real machine
and reports every difference, and ``resume_plan`` only ever proposes safe steps (open what exists). It never closes, moves, saves or
overwrites anything, and never replays an unfinished action: a step that was running when JARVIS stopped is reported as UNCERTAIN.

Privacy: window titles are not stored (they can hold private text) - only the program and its path. Stored in the existing settings
store (no new database); at most 20 snapshots, the oldest dropped. Pure functions take a ``Live`` view of the machine, so they are tested
with made-up machines.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

KEY = "work_snapshots"
MAX_SNAPSHOTS = 20
MAX_FILES = 20
MAX_WINDOWS = 20
_NAME = re.compile(r"^[\w][\w .'-]{0,39}$", re.UNICODE)


@dataclass
class Live:
    """What the machine looks like now. Replaced by fakes in tests."""

    windows: Callable[[], list[dict[str, Any]]] = lambda: []                    # [{"process","path"}]
    exists: Callable[[str], bool] = os.path.exists
    mtime: Callable[[str], float | None] = lambda p: (os.path.getmtime(p) if os.path.exists(p) else None)
    mission: Callable[[str], dict[str, Any] | None] = lambda mid: None          # {"state","done","total","next"} or None
    now: Callable[[], float] = time.time


@dataclass
class Finding:
    kind: str            # ok | missing | changed | gone | info | uncertain
    what: str
    detail: str = ""


def capture(*, name: str, folder: str = "", files: list[str] | None = None, windows: list[dict[str, Any]] | None = None,
            mission: dict[str, Any] | None = None, questions: list[str] | None = None, notes: str = "",
            mtime: Callable[[str], float | None] = lambda p: None, previous_version: int = 0) -> dict[str, Any]:
    if not _NAME.match(name or ""):
        raise ValueError("Give the snapshot a short name (letters, digits, spaces).")
    return {
        "name": name.strip(), "version": previous_version + 1, "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "folder": folder, "files": [{"path": f, "mtime": mtime(f)} for f in (files or [])[:MAX_FILES]],
        "windows": [{"process": str(w.get("process", ""))[:60], "path": str(w.get("path", ""))[:300]}
                    for w in (windows or [])[:MAX_WINDOWS] if w.get("process")],      # no titles on purpose
        "mission": mission, "questions": [q[:200] for q in (questions or [])[:10]], "notes": notes[:500],
    }


def compare(snap: dict[str, Any], live: Live) -> list[Finding]:
    out: list[Finding] = []
    folder = snap.get("folder", "")
    if folder:
        out.append(Finding("ok" if live.exists(folder) else "missing", f"folder {folder}", "" if live.exists(folder) else "it no longer exists"))
    for f in snap.get("files", []):
        path, then = f["path"], f.get("mtime")
        now = live.mtime(path)
        if now is None:
            out.append(Finding("missing", f"file {path}", "moved or deleted"))
        elif then is not None and abs(now - then) > 1:
            out.append(Finding("changed", f"file {path}", "edited since the snapshot (by you or another program)"))
        else:
            out.append(Finding("ok", f"file {path}"))
    running = {(w.get("process", "").lower(), w.get("path", "").lower()) for w in live.windows()}
    names = {p for p, _ in running}
    for w in snap.get("windows", []):
        proc = w["process"].lower()
        if proc in names:
            out.append(Finding("ok", f"{w['process']} is open"))
        elif w["path"] and live.exists(w["path"]):
            out.append(Finding("gone", f"{w['process']} is not open", "can be started again; its unsaved work (if any) is not recoverable by JARVIS"))
        else:
            out.append(Finding("missing", f"{w['process']} is not open", "and it can no longer be found to start"))
    m = snap.get("mission")
    if m:
        now = live.mission(m["id"])
        if now is None:
            out.append(Finding("missing", f"mission '{m.get('title', '')}'", "it no longer exists"))
        else:
            if now["state"] in ("running", "verifying"):
                out.append(Finding("uncertain", f"mission '{m.get('title', '')}'",
                                   f"was running when JARVIS stopped; step {now.get('next', '?')} may or may not have happened - check it before continuing"))
            elif now["state"] != m.get("state") or now.get("done") != m.get("done"):
                out.append(Finding("changed", f"mission '{m.get('title', '')}'",
                                   f"now {now['state']}, {now.get('done')}/{now.get('total')} steps (was {m.get('state')}, {m.get('done')}/{m.get('total')})"))
            else:
                out.append(Finding("ok", f"mission '{m.get('title', '')}'", f"{now['state']}, {now.get('done')}/{now.get('total')} steps"))
    if snap.get("questions"):
        out.append(Finding("info", "open questions", "; ".join(snap["questions"])))
    return out


def resume_plan(snap: dict[str, Any], findings: list[Finding]) -> dict[str, Any]:
    """Safe steps only: open what still exists. Anything uncertain or changed needs your decision; nothing is closed or overwritten."""
    steps: list[dict[str, Any]] = []
    folder = snap.get("folder", "")
    if folder and not any(f.kind == "missing" and f.what == f"folder {folder}" for f in findings):
        steps.append({"tool": "open_path", "arguments": {"path": folder}})
    missing = {f.what for f in findings if f.kind == "missing"}
    for f in snap.get("files", []):
        if f"file {f['path']}" not in missing:
            steps.append({"tool": "open_path", "arguments": {"path": f["path"]}})
    ask = [f for f in findings if f.kind in ("uncertain", "changed")]
    return {"steps": steps, "needs_your_decision": [f"{f.what}: {f.detail}" for f in ask],
            "skipped": [f"{f.what}: {f.detail}" for f in findings if f.kind in ("missing", "gone")]}


def describe(snap: dict[str, Any], findings: list[Finding]) -> str:
    icon = {"ok": "OK", "missing": "MISSING", "changed": "CHANGED", "gone": "NOT OPEN", "info": "NOTE", "uncertain": "UNCERTAIN"}
    lines = [f"Snapshot '{snap['name']}' v{snap['version']} from {snap['taken_at'][:16].replace('T', ' ')} UTC - compared with this PC now:"]
    lines += [f"  [{icon[f.kind]}] {f.what}" + (f" - {f.detail}" if f.detail else "") for f in findings]
    if snap.get("notes"):
        lines.append(f"Notes: {snap['notes']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------- storage
def _load(store: Any) -> list[dict[str, Any]]:
    data = store.get(KEY, [])
    return data if isinstance(data, list) else []


def save(store: Any, snap: dict[str, Any]) -> None:
    items = [s for s in _load(store) if not (s["name"].lower() == snap["name"].lower() and s["version"] == snap["version"])]
    items.append(snap)
    store.set(KEY, items[-MAX_SNAPSHOTS:])


def latest(store: Any, name: str = "") -> dict[str, Any] | None:
    items = [s for s in _load(store) if not name or s["name"].lower() == name.lower()]
    return max(items, key=lambda s: s["taken_at"], default=None)


def versions(store: Any, name: str) -> list[dict[str, Any]]:
    return sorted((s for s in _load(store) if s["name"].lower() == name.lower()), key=lambda s: s["version"])


def delete(store: Any, name: str) -> int:
    items = _load(store)
    keep = [s for s in items if s["name"].lower() != name.lower()]
    store.set(KEY, keep)
    return len(items) - len(keep)


def next_version(store: Any, name: str) -> int:
    return max((s["version"] for s in versions(store, name)), default=0)


def mission_view(db: Any) -> Callable[[str], dict[str, Any] | None]:
    from app.core.missions import MissionStore

    def view(mid: str) -> dict[str, Any] | None:
        try:
            m = MissionStore(db).get(mid)
        except Exception:
            return None
        done = sum(1 for s in m.steps if s.state in ("done", "skipped"))
        nxt = m.next_step
        return {"state": m.state, "done": done, "total": len(m.steps), "next": nxt.n if nxt else None, "title": m.title}

    return view


def recovery_report(db: Any) -> list[dict[str, Any]]:
    """After a restart or crash: every unfinished mission with its last confirmed step, what is uncertain, and the safe options."""
    from app.core.missions import MissionStore

    out = []
    for m in MissionStore(db).recent(10, ("paused", "running", "verifying", "planned")):
        confirmed = [s for s in m.steps if s.state in ("done", "skipped")]
        uncertain = [s for s in m.steps if s.state == "running"]
        pending_checkpoint = next((s for s in m.steps if s.checkpoint and s.state == "pending"), None)
        risky = next((s for s in m.steps if s.state in ("pending", "failed", "running") and s.level >= 1 and not s.reversible), None)
        options = ["show the plan", "cancel the mission"]
        if not uncertain:
            options.insert(0, "continue from the last confirmed step")
        else:
            options.insert(0, "check what step %d did on the PC, then continue" % uncertain[0].n)
        out.append({"id": m.id, "title": m.title, "state": m.state, "note": m.note,
                    "last_confirmed": f"step {confirmed[-1].n}: {confirmed[-1].title}" if confirmed else "none yet",
                    "uncertain": [f"step {s.n}: {s.title} ({s.tool}) - it was running; JARVIS cannot tell whether it finished" for s in uncertain],
                    "waiting_for_you": f"go-ahead before step {pending_checkpoint.n}: {pending_checkpoint.title}" if pending_checkpoint else "",
                    "needs_fresh_confirmation": f"step {risky.n}: {risky.title} changes something that cannot be undone, so it asks again" if risky else "",
                    "options": options})
    return out


def recovery_text(report: list[dict[str, Any]]) -> str:
    if not report:
        return "Nothing was left unfinished."
    lines = []
    for r in report:
        lines.append(f"Unfinished: '{r['title']}' ({r['state']}). Last confirmed: {r['last_confirmed']}.")
        lines += [f"  UNCERTAIN - {u}" for u in r["uncertain"]]
        for k in ("waiting_for_you", "needs_fresh_confirmation"):
            if r[k]:
                lines.append(f"  {r[k]}")
        lines.append("  You can: " + "; ".join(r["options"]) + ".")
    return "\n".join(lines)
