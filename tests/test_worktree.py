from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.core import worktree
from app.tools.base import ToolError
from app.tools.developer import looks_secret

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def run(argv: list[str], cwd: Path) -> tuple[int, str]:
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def git(cwd: Path, *args: str) -> str:
    code, out = run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd)
    assert code == 0, out
    return out


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("one\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path


def runner(cwd_run=run):
    def r(argv, cwd):
        if argv[:2] == ["git", "commit"] or argv[:2] == ["git", "merge"]:
            argv = ["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv[1:]]
        return cwd_run(argv, cwd)
    return r


def test_start_edit_status_merge_discard(repo: Path) -> None:
    r = runner()
    tree = worktree.start(r, repo, "New Login!")
    assert tree.name == "new-login" and tree.folder == repo.parent / "proj.jarvis-new-login" and tree.base_branch == "main"
    (tree.folder / "a.txt").write_text("one\ntwo\n")
    (tree.folder / "b.txt").write_text("new\n")
    (tree.folder / ".env").write_text("TOKEN=abc\n")
    assert (repo / "a.txt").read_text() == "one\n"  # the real project is untouched
    assert worktree.listing(r, repo) == ["new-login"]
    info = worktree.status(r, repo, "new-login")
    assert len(info["uncommitted"]) == 3 and info["commits_ahead"] == 0
    done = worktree.merge(r, repo, "new-login", "Add login", looks_secret)
    assert done["merged"] and done["held_back"] == [".env"]
    assert (repo / "a.txt").read_text() == "one\ntwo\n" and (repo / "b.txt").exists() and not (repo / ".env").exists()
    worktree.discard(r, repo, "new-login")
    assert worktree.listing(r, repo) == [] and not tree.folder.exists()


def test_discard_throws_the_work_away(repo: Path) -> None:
    r = runner()
    tree = worktree.start(r, repo, "tmp")
    (tree.folder / "x.txt").write_text("x")
    worktree.discard(r, repo, "tmp")
    assert not (repo / "x.txt").exists() and "jarvis/tmp" not in git(repo, "branch")


def test_merge_refuses_when_your_tree_is_dirty(repo: Path) -> None:
    r = runner()
    tree = worktree.start(r, repo, "exp")
    (tree.folder / "n.txt").write_text("n")
    (repo / "a.txt").write_text("my own edit\n")
    with pytest.raises(ToolError, match="uncommitted"):
        worktree.merge(r, repo, "exp", "m", looks_secret)
    assert (repo / "a.txt").read_text() == "my own edit\n"


def test_conflicts_are_aborted_and_leave_the_project_clean(repo: Path) -> None:
    r = runner()
    tree = worktree.start(r, repo, "exp")
    (tree.folder / "a.txt").write_text("from experiment\n")
    (repo / "a.txt").write_text("from main\n")
    git(repo, "commit", "-q", "-am", "main change")
    with pytest.raises(ToolError, match="conflicts"):
        worktree.merge(r, repo, "exp", "m", looks_secret)
    assert git(repo, "status", "--short").strip() == "" and (repo / "a.txt").read_text() == "from main\n"


def test_nothing_to_merge_and_bad_names(repo: Path) -> None:
    r = runner()
    worktree.start(r, repo, "empty")
    assert worktree.merge(r, repo, "empty", "m", looks_secret)["merged"] is False
    with pytest.raises(ToolError):
        worktree.start(r, repo, "empty")  # already exists
    with pytest.raises(ToolError):
        worktree.find(r, repo, "missing")
    with pytest.raises(ToolError):
        worktree.clean_name("!!!")


def test_detached_head_is_refused(repo: Path) -> None:
    git(repo, "checkout", "-q", "--detach")
    with pytest.raises(ToolError, match="detached"):
        worktree.start(runner(), repo, "x")
