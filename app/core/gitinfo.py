"""Read-only git facts for the event diagnosis: what changed in a project before something failed, and what is uncommitted now.

Runs only ``git log`` / ``git status`` / ``git diff --stat``-style READS through an injected ``run(argv, cwd) -> (returncode, output)`` (the app passes the
reviewed command executor; tests pass a fake). It never commits, checks out, resets, fetches or writes anything. Commit messages and file names are
untrusted text: they are shortened and only printed, never run. Not a git repository, no git installed, or a failing command = "no information",
which the diagnosis then says out loud instead of guessing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

Run = Callable[[list[str], Path], tuple[int, str]]
MAX_FILES = 30


def _clean(text: str, limit: int = 120) -> str:
    return " ".join(text.split())[:limit]


def changes_since(run: Run, folder: Path, when: datetime) -> list[str]:
    """Files changed by commits since ``when`` plus files with uncommitted changes (marked), newest information first. [] = nothing known."""
    out: list[str] = []
    stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rc, text = run(["git", "log", f"--since={stamp}", "--name-only", "--pretty=format:", "--no-color"], folder)
        if rc == 0:
            out += [_clean(line, 200) for line in text.splitlines() if line.strip()]
        rc, text = run(["git", "status", "--short"], folder)
        if rc == 0:
            for line in text.splitlines():
                name = line[3:].strip().strip('"')
                if name:
                    out.append(f"{_clean(name, 200)} (uncommitted)")
    except Exception:
        return []
    seen: dict[str, None] = {}
    for item in out:
        seen.setdefault(item, None)
    return list(seen)[:MAX_FILES]


def last_commits(run: Run, folder: Path, count: int = 3) -> list[str]:
    try:
        rc, text = run(["git", "log", f"-n{int(count)}", "--pretty=format:%h %s", "--no-color"], folder)
    except Exception:
        return []
    return [_clean(line) for line in text.splitlines() if line.strip()] if rc == 0 else []


def event_context(resolve_folder: Callable[[str], Path | None], run: Run, count_before: Callable[[str], int]):
    """An ``event_intel.Context`` whose readers use git (resolve_folder maps a project name to its folder, or None)."""
    from app.core.event_intel import Context

    def before(project: str, when: datetime) -> list[str]:
        folder = resolve_folder(project)
        return changes_since(run, folder, when) if folder is not None else []

    return Context(changes_before=before, seen_before=count_before)


def count_similar(db, signature: str) -> int:
    """How many stored events had the same name and project as a situation's signature (category:project:name)."""
    try:
        _, project, name = signature.split(":", 2)
    except ValueError:
        return 0
    with db.connect() as conn:
        rows = conn.execute("SELECT data FROM events WHERE name = ?", (name,)).fetchall()
    import json

    n = 0
    for r in rows:
        try:
            if str(json.loads(r["data"]).get("project", "")) == project:
                n += 1
        except (ValueError, AttributeError):
            continue
    return max(0, n - 1)           # the event being diagnosed is one of them
