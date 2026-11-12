"""What would break if this project folder moved? A static check of the folder - nothing is moved, run or simulated.

It looks for things that store the folder's own absolute location:
* a Python virtual environment inside it (``pyvenv.cfg`` and the launchers under Scripts / bin hold absolute paths - a moved venv must be recreated);
* text files that contain the folder's current path (settings, .env, IDE and tool configuration, scripts) - listed with the line number;
* a Git worktree or submodule pointer (``.git`` as a FILE holds a path);
* links / junctions inside the folder (they keep pointing at the old place);
* programs running from inside the folder right now (they must be closed first), when a process reader is given.
Reported as: will break / will probably be fine / not checked. This is static analysis: it cannot see paths stored OUTSIDE the folder (shortcuts,
scheduled tasks, other projects' settings, the registry, environment variables) and says so. Reading is bounded (files, size, depth); binary files and
dependency folders are skipped; only path and line number are reported, never file contents.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MAX_FILES, MAX_BYTES, MAX_HITS = 4000, 512_000, 40
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", "target", ".gradle", ".idea"}
TEXT_EXT = {".py", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".env", ".txt", ".md", ".bat", ".cmd", ".ps1", ".sh", ".xml", ".js", ".ts", ".code-workspace", ".conf", ".properties", ".gradle", ".csproj", ".sln", ".lock", ""}


@dataclass
class Finding:
    kind: str            # venv | path_in_file | git_pointer | link | running
    where: str
    detail: str
    breaks: bool


@dataclass
class Report:
    folder: str
    findings: list[Finding] = field(default_factory=list)
    files_read: int = 0
    truncated: bool = False
    not_checked: list[str] = field(default_factory=list)


def _variants(folder: Path) -> list[str]:
    s = str(folder)
    return sorted({v.lower() for v in (s, s.replace("\\", "/"), s.replace("\\", "\\\\"), s.replace("/", "\\"))}, key=len, reverse=True)


def check(folder: str, processes: Callable[[], list[tuple[str, str]]] | None = None) -> Report:
    root = Path(folder).expanduser()
    rep = Report(str(root))
    if not root.is_dir():
        raise FileNotFoundError(f"'{folder}' is not a folder")
    root = root.resolve()
    needles = _variants(root)
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        for d in list(dirnames):
            p = here / d
            if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
                rep.findings.append(Finding("link", str(p.relative_to(root)), "a link or junction: it keeps pointing at its old target", True))
                dirnames.remove(d)
            elif (p / "pyvenv.cfg").is_file():
                rep.findings.append(Finding("venv", str(p.relative_to(root)), "a Python virtual environment: it stores absolute paths and must be recreated after a move", True))
                dirnames.remove(d)
            elif d in SKIP_DIRS:
                dirnames.remove(d)
        if (here / ".git").is_file():
            rep.findings.append(Finding("git_pointer", str((here / ".git").relative_to(root)), "a Git worktree / submodule pointer file: it holds a path that may need repair (git worktree repair)", True))
        for name in filenames:
            p = here / name
            if p.suffix.lower() not in TEXT_EXT and not name.startswith(".env"):
                continue
            if rep.files_read >= MAX_FILES:
                rep.truncated = True
                break
            try:
                if p.stat().st_size > MAX_BYTES:
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rep.files_read += 1
            low = text.lower()
            if not any(n in low for n in needles):
                continue
            for i, line in enumerate(low.splitlines(), 1):
                if any(n in line for n in needles):
                    if sum(1 for f in rep.findings if f.kind == "path_in_file") < MAX_HITS:
                        rep.findings.append(Finding("path_in_file", f"{p.relative_to(root)}:{i}", "contains this folder's current absolute path", True))
                    else:
                        rep.truncated = True
                    break
        if rep.truncated and rep.files_read >= MAX_FILES:
            break
    if processes is not None:
        try:
            for name, cwd in processes():
                if cwd and str(cwd).lower().startswith(str(root).lower()):
                    rep.findings.append(Finding("running", name, "is running from inside this folder: close it before moving", True))
        except Exception:
            rep.not_checked.append("running programs (could not be read)")
    else:
        rep.not_checked.append("running programs")
    rep.not_checked += ["shortcuts, scheduled tasks and other programs' settings that point here (they are outside this folder)", "environment variables and the registry",
                        "whether the project still builds and runs after the move (run its tests afterwards)"]
    return rep


def describe(rep: Report, destination: str = "") -> str:
    head = f"Static check of {rep.folder}" + (f" before moving it to {destination}" if destination else "") + f" ({rep.files_read} text files read; nothing was moved or run):"
    if not rep.findings:
        body = ["Nothing inside the folder stores its own location. The move itself should not break files in it."]
    else:
        order = {"running": 0, "venv": 1, "git_pointer": 2, "link": 3, "path_in_file": 4}
        body = [f"WILL NEED ATTENTION ({len(rep.findings)}):"] + [f"  [{f.kind.replace('_', ' ')}] {f.where} - {f.detail}" for f in sorted(rep.findings, key=lambda f: order.get(f.kind, 9))[:30]]
        plan = ["Suggested order: 1) close programs running from the folder; 2) move the folder; 3) recreate any virtual environment and reinstall its packages; "
                "4) update the listed files to the new path; 5) run the project's tests. Keep the old copy until step 5 passes."]
        body += plan
    tail = ["Not checked: " + "; ".join(rep.not_checked) + "."]
    if rep.truncated:
        tail.append("The scan stopped at its limit, so more files may contain the path.")
    return "\n".join([head, *body, *tail])


def running_processes() -> list[tuple[str, str]]:
    import psutil

    out = []
    for p in psutil.process_iter(["name", "cwd"]):
        try:
            out.append((p.info["name"] or "", p.info["cwd"] or ""))
        except Exception:
            continue
    return out
