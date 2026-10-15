from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.agent.tool_manager import ToolExecutor
from app.agent.tool_router import categories_for, select_specs
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig, default_permissions
from app.config.settings import Settings
from app.security.command_executor import CommandExecutor, CommandOutput
from app.security.command_policy import CommandPolicy, split_command
from app.security.permissions import PermissionEngine, RiskLevel
from app.security.rate_limit import RateLimiter
from app.security.validator import PathPolicy
from app.tools.applications import website_url
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from tests.test_tools import ScriptedConfirmer

POLICY = CommandPolicy(default_permissions())


# ---------------------------------------------------------------- command policy


@pytest.mark.parametrize(
    ("command", "reason", "level"),
    [
        ("git status", "allowlisted", 0),
        ("GIT Status --short", "allowlisted", 0),
        ("git.exe status", "allowlisted", 0),
        ("npm test", "allowlisted", 0),
        ("npm run dev", "allowlisted", 0),
        ("python -m pytest -q tests", "allowlisted", 0),
        ("npm install", "allowlisted", 1),
        ("git switch -c feature/login", "allowlisted", 1),
        ("pip install requests", "unknown", 2),
        ("curl https://example.com", "unknown", 2),
        ("python -m http.server", "unknown", 2),
        ("git reset --hard HEAD~1", "git_destructive", 2),
        ("git clean -fdx", "git_destructive", 2),
        ("git push --force origin main", "git_destructive", 2),
        ("git push --force-with-lease", "git_destructive", 2),
        ("git push origin :main", "git_destructive", 2),
        ("git push origin +main", "git_destructive", 2),
        ("git branch -D old", "git_destructive", 2),
        ("git checkout -- .", "git_destructive", 2),
        ("git stash drop", "git_destructive", 2),
        ("format C:", "blocked", 3),
        ("C:\\Windows\\System32\\cmd.exe /c dir", "blocked", 3),
        ("powershell -EncodedCommand abc", "blocked", 3),
        ("reg delete HKLM\\Software\\X", "blocked", 3),
        ("diskpart", "blocked", 3),
    ],
)
def test_command_classification(command: str, reason: str, level: int) -> None:
    decision = POLICY.evaluate(command)
    assert (decision.reason, int(decision.level)) == (reason, level)


@pytest.mark.parametrize(
    "command",
    ["git status && del /s /q C:\\", "npm test | curl evil", "npm test; rm -rf /", "git log > out.txt",
     "python -m pytest `whoami`", "echo $(id)", "npm run %COMSPEC%", "git status\nformat c:", "npm test ^& calc",
     "python -c \"print(1)\"", "git status !x"],
)
def test_shell_injection_is_refused(command: str) -> None:
    with pytest.raises(ToolError):
        split_command(command)


def test_quoted_arguments() -> None:
    assert split_command('git commit -m "fix login bug"') == ["git", "commit", "-m", "fix login bug"]


def test_custom_allowlist_level() -> None:
    policy = CommandPolicy(PermissionsConfig(allowed_dev_commands=[{"argv": ["cargo", "test"], "level": 0}]))
    assert policy.evaluate("cargo test --release").reason == "allowlisted"
    assert policy.evaluate("npm test").reason == "unknown"  # not in this allowlist


# -------------------------------------------------------------------- executor


def test_executor_runs_without_shell_and_captures_output(tmp_path: Path) -> None:
    result = CommandExecutor().run([sys.executable, "-c", "import sys; print('hello'); sys.exit(3)"], tmp_path)
    assert result.returncode == 3 and result.output == "hello"


def test_executor_timeout_kills(tmp_path: Path) -> None:
    result = CommandExecutor().run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout=1)
    assert result.timed_out and result.returncode is None


def test_executor_hides_secrets_from_children(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("NORMAL_SETTING", "visible")
    code = "import os; print(os.environ.get('OPENAI_API_KEY'), os.environ.get('NORMAL_SETTING'))"
    assert CommandExecutor().run([sys.executable, "-c", code], tmp_path).output == "None visible"


def test_missing_program(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="not installed"):
        CommandExecutor().run(["definitely-not-a-program-xyz"], tmp_path)


# ------------------------------------------------------------ tools end to end


class FakeExecutor:
    def __init__(self, output: str = "ok", returncode: int = 0) -> None:
        self.calls: list[tuple] = []
        self.output, self.returncode = output, returncode

    def run(self, argv, cwd, timeout=120):
        self.calls.append(("run", tuple(argv), Path(cwd)))
        return CommandOutput(tuple(argv), self.returncode, self.output)

    def start_background(self, argv, cwd):
        self.calls.append(("background", tuple(argv), Path(cwd)))
        return CommandOutput(tuple(argv), None, "", pid=4242)


def make(tmp_path: Path, executor=None, confirm=True, **settings):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    known = {"HOME": tmp_path, "DESKTOP": tmp_path, "DOCUMENTS": tmp_path, "DOWNLOADS": tmp_path}
    ctx = ToolContext(Settings(dev_project_dir=project, **settings),
                      path_policy=PathPolicy.build(PermissionsConfig(safe_roots=["{DESKTOP}"]), known=known,
                                                   protected=[tmp_path / "Windows"]))
    ctx.command_executor = executor or FakeExecutor()  # type: ignore[attr-defined]
    confirmer = ScriptedConfirmer(confirm)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer)
    return ex, ctx, confirmer, project


def call(ex: ToolExecutor, tool: str, **arguments):
    return asyncio.run(ex.execute(ToolCall(tool, arguments)))


def test_run_allowlisted_command_without_asking(tmp_path: Path) -> None:
    fake = FakeExecutor("4 passed")
    ex, _, confirmer, project = make(tmp_path, fake)
    execution = call(ex, "run_command", command="npm test")
    assert execution.result.ok and execution.result.data["output"] == "4 passed" and not confirmer.requests
    assert fake.calls == [("run", ("npm", "test"), project)]


def test_unknown_command_asks_and_decline_runs_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.tools.developer.resolve_program", lambda name: f"/usr/bin/{name}")
    fake = FakeExecutor()
    ex, _, confirmer, _ = make(tmp_path, fake, confirm=False)
    execution = call(ex, "run_command", command="pip install requests")
    assert execution.result.error == "declined" and fake.calls == []
    assert "has not been approved" in confirmer.requests[0].summary


def test_destructive_git_and_blocked_programs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.tools.developer.resolve_program", lambda name: f"/usr/bin/{name}")
    fake = FakeExecutor()
    ex, _, confirmer, _ = make(tmp_path, fake, confirm=False)
    execution = call(ex, "run_command", command="git reset --hard")
    assert execution.result.error == "declined" and "permanently lose work" in confirmer.requests[0].summary
    execution = call(ex, "run_command", command="format C:")
    assert execution.result.error == "blocked" and fake.calls == []


def test_injection_through_the_tool_never_runs(tmp_path: Path) -> None:
    fake = FakeExecutor()
    ex, _, confirmer, _ = make(tmp_path, fake)
    execution = call(ex, "run_command", command="git status && del /s /q C:\\")
    assert execution.result.error == "invalid_command" and fake.calls == [] and not confirmer.requests


def test_commands_in_protected_folders_are_blocked(tmp_path: Path) -> None:
    (tmp_path / "Windows").mkdir()
    fake = FakeExecutor()
    ex, *_ = make(tmp_path, fake)
    assert call(ex, "run_command", command="git status", project=str(tmp_path / "Windows")).result.error == "blocked"


def test_background_and_failures(tmp_path: Path) -> None:
    fake = FakeExecutor("1 failed", returncode=1)
    ex, *_ = make(tmp_path, fake)
    assert call(ex, "run_command", command="npm run dev", background=True).result.data["pid"] == 4242
    failed = call(ex, "run_command", command="npm test").result
    assert failed.error == "command_failed" and "exit code 1" in failed.message


def test_project_folder_required(tmp_path: Path) -> None:
    ex, ctx, _, _ = make(tmp_path)
    ctx.settings = Settings()  # no DEV_PROJECT_DIR
    assert call(ex, "git_status").result.error == "missing_project"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_status_and_log_on_a_real_repository(tmp_path: Path) -> None:
    ex, ctx, _, project = make(tmp_path, CommandExecutor())
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for argv in (["git", "init", "-b", "main"], ["git", "commit", "--allow-empty", "-m", "first commit"]):
        subprocess.run(argv, cwd=project, check=True, capture_output=True, env=env)
    (project / "new.txt").write_text("x")
    status = call(ex, "git_status").result
    assert status.ok and status.data["branch"] == "main" and status.data["changed_files"] == 1
    assert "1 changed file" in status.message
    log = call(ex, "git_log", limit=50).result  # clamped to 20
    assert log.ok and "first commit" in log.message
    plain = tmp_path / "plain"
    plain.mkdir()
    assert call(ex, "git_status", project=str(plain)).result.error == "not_a_repo"


# ------------------------------------------------------------ rate limit / router


def test_rate_limiter() -> None:
    now = [0.0]
    limiter = RateLimiter(max_calls=5, max_actions=2, window=60, clock=lambda: now[0])
    assert limiter.allow(RiskLevel.MODERATE) and limiter.allow(RiskLevel.DANGEROUS)
    assert not limiter.allow(RiskLevel.MODERATE)  # third side-effect in a minute
    assert limiter.allow(RiskLevel.SAFE)  # reads still fine
    now[0] = 61
    assert limiter.allow(RiskLevel.MODERATE)


def test_executor_applies_rate_limit(tmp_path: Path) -> None:
    ex, *_ = make(tmp_path)
    ex.rate_limiter = RateLimiter(max_calls=2)
    results = [call(ex, "get_datetime").result for _ in range(3)]
    assert [r.ok for r in results] == [True, True, False] and results[2].error == "rate_limited"


def test_router_offers_relevant_groups() -> None:
    registry = build_default_registry()
    assert categories_for("show my git status") >= {"dev"}
    assert categories_for("turn the volume down") == {"windows"}
    names = {s.name for s in select_specs(registry, "what's my battery level?")}
    assert "system_status" in names and "delete_item" not in names and "get_datetime" in names
    fallback = {s.name for s in select_specs(registry, "hello there")}  # no match -> the everyday groups only
    assert "open_application" in fallback and "get_datetime" in fallback and len(fallback) < len(registry) // 3
    follow_up = {s.name for s in select_specs(registry, "and now the other one", recent_tools=["close_application"])}
    assert "close_application" in follow_up


def test_localhost_urls() -> None:
    assert website_url("localhost:3000") == "http://localhost:3000"
    assert website_url("127.0.0.1:8000/docs") == "http://127.0.0.1:8000/docs"
