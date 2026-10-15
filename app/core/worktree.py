"""Risky edits in their own Git worktree: a separate folder and branch, so the real project is never touched
until you accept the result.

Flow: ``start`` makes a sibling folder ``<project>.jarvis-<name>`` on a new branch ``jarvis/<name>``; edits and tests
run there (give that folder as the project); ``merge`` commits what is in it (secret-looking files are left out) and
merges the branch into the branch you were on - only when your own working tree is clean, and a conflict is aborted
so nothing is left half-merged; ``discard`` removes the folder and branch. Nothing is ever pushed.

``run`` is any function ``(argv, cwd) -> (returncode, output)`` (tests use real git; the tool uses the command executor).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.tools.base import ToolError

Runner = Callable[[list[str], Path], tuple[int, str]]
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
BRANCH_PREFIX = "jarvis/"


@dataclass(frozen=True)
class Worktree:
    name: str
    folder: Path
    branch: str
    base_branch: str


def clean_name(text: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")[:30].strip("-")
    if not _NAME.match(name):
        raise ToolError("Give the experiment a short name, like 'new-login'.", "invalid_name")
    return name


def _git(run: Runner, cwd: Path, *args: str) -> str:
    code, output = run(["git", *args], cwd)
    if code != 0:
        raise ToolError(f"git {args[0]} failed: {output.strip()[-300:]}", "git_failed")
    return output


def _location(repo: Path, name: str) -> Path:
    return repo.parent / f"{repo.name}.jarvis-{name}"


def current_branch(run: Runner, repo: Path) -> str:
    branch = _git(run, repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch == "HEAD":
        raise ToolError("The project isn't on a branch (detached HEAD); switch to a branch first.", "detached")
    return branch


def start(run: Runner, repo: Path, name: str) -> Worktree:
    name = clean_name(name)
    base = current_branch(run, repo)
    folder = _location(repo, name)
    if folder.exists():
        raise ToolError(f"{folder.name} already exists. Merge or discard it first, or pick another name.", "exists")
    _git(run, repo, "worktree", "add", "-b", BRANCH_PREFIX + name, str(folder), "HEAD")
    return Worktree(name, folder, BRANCH_PREFIX + name, base)


def find(run: Runner, repo: Path, name: str) -> Worktree:
    name = clean_name(name)
    folder = _location(repo, name)
    listing = _git(run, repo, "worktree", "list", "--porcelain")
    if not any(line == f"branch refs/heads/{BRANCH_PREFIX}{name}" for line in listing.splitlines()) or not folder.is_dir():
        raise ToolError(f"There is no experiment called '{name}' for this project.", "not_found")
    return Worktree(name, folder, BRANCH_PREFIX + name, current_branch(run, repo))


def listing(run: Runner, repo: Path) -> list[str]:
    out = _git(run, repo, "worktree", "list", "--porcelain")
    return [line.split(BRANCH_PREFIX, 1)[1] for line in out.splitlines()
            if line.startswith(f"branch refs/heads/{BRANCH_PREFIX}")]


def status(run: Runner, repo: Path, name: str) -> dict:
    tree = find(run, repo, name)
    changed = [line for line in _git(run, tree.folder, "status", "--short").splitlines() if line.strip()]
    ahead = _git(run, repo, "rev-list", "--count", f"{tree.base_branch}..{tree.branch}").strip()
    stat = _git(run, repo, "diff", "--stat", f"{tree.base_branch}...{tree.branch}").strip().splitlines()
    return {"name": tree.name, "folder": str(tree.folder), "branch": tree.branch, "base": tree.base_branch,
            "uncommitted": changed, "commits_ahead": int(ahead or 0), "summary": stat[-1].strip() if stat else ""}


def merge(run: Runner, repo: Path, name: str, message: str, is_secret: Callable[[str], bool]) -> dict:
    tree = find(run, repo, name)
    if [line for line in _git(run, repo, "status", "--short").splitlines() if line.strip()]:
        raise ToolError("Your project has uncommitted changes of its own. Commit or set them aside first, "
                        "so the merge can't mix with them.", "dirty")
    held_back: list[str] = []
    changed = [line for line in _git(run, tree.folder, "status", "--short").splitlines() if line.strip()]
    if changed:
        _git(run, tree.folder, "add", "-A")
        for line in changed:
            path = line[3:].split(" -> ")[-1].strip().strip('"')
            if is_secret(path):
                _git(run, tree.folder, "reset", "-q", "--", path)
                held_back.append(path)
        if _git(run, tree.folder, "diff", "--cached", "--name-only").strip():
            _git(run, tree.folder, "commit", "-m", message)
    ahead = int(_git(run, repo, "rev-list", "--count", f"{tree.base_branch}..{tree.branch}").strip() or 0)
    if ahead == 0:
        return {"merged": False, "commits": 0, "held_back": held_back, "note": "There was nothing to merge."}
    code, output = run(["git", "merge", "--no-ff", "-m", f"Merge experiment {name}: {message}", tree.branch], repo)
    if code != 0:
        run(["git", "merge", "--abort"], repo)  # never leave the project half-merged
        raise ToolError(f"The experiment conflicts with changes made since it started; nothing was merged and your "
                        f"project is unchanged. ({output.strip()[-160:]})", "conflict")
    return {"merged": True, "commits": ahead, "held_back": held_back, "note": ""}


def discard(run: Runner, repo: Path, name: str) -> None:
    tree = find(run, repo, name)
    _git(run, repo, "worktree", "remove", "--force", str(tree.folder))
    _git(run, repo, "branch", "-D", tree.branch)
