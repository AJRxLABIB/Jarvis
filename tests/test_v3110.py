"""3.11.0 Phase 8: the evolution engine - evidence, proposals, the lab (a copy), approval, backup, rollback."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import emergency
from app.evolution import analyze, deploy, lab
from app.evolution.proposals import Edit, ProposalStore, apply_edits, validate
from app.memory.database import Database
from app.security.command_executor import CommandOutput
from app.security.permissions import PermissionEngine
from app.tools import evolution_tools
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from app.utils.paths import AppPaths
from tests.test_tools import ScriptedConfirmer

ORIGINAL = 'def greet():\n    return "hi"\n'


@pytest.fixture()
def world(tmp_path: Path):
    source = tmp_path / "src"
    (source / "app" / "tools").mkdir(parents=True)
    (source / "app" / "security").mkdir()
    (source / "tests").mkdir()
    (source / "app" / "__init__.py").write_text("")
    (source / "app" / "tools" / "greet.py").write_text(ORIGINAL)
    (source / "app" / "security" / "permissions.py").write_text("LEVEL = 2\n")
    (source / "tests" / "test_greet.py").write_text("def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
    (source / ".venv").mkdir()
    (source / ".venv" / "junk.txt").write_text("never copied")
    database = Database(tmp_path / "j.db")
    database.initialize()
    paths = AppPaths.build(tmp_path / "root")
    ctx = ToolContext(Settings(jarvis_source_dir=source), paths=paths, database=database,
                      clock=lambda: datetime(2026, 9, 26, 12, tzinfo=timezone.utc))
    return source, ctx


class Runner:
    """Stands in for CommandExecutor: records the argv and answers like pytest / ruff."""

    def __init__(self, summary: str = "12 passed, 1 skipped in 3.2s", lint: int = 0) -> None:
        self.summary, self.lint, self.calls = summary, lint, []

    def run(self, argv, cwd, timeout=120.0):
        self.calls.append((list(argv), Path(cwd)))
        if "pytest" in argv:
            return CommandOutput(tuple(argv), 0, "....\n" + self.summary)
        return CommandOutput(tuple(argv), self.lint, "All checks passed!" if not self.lint else "E501")


def execute(ctx, tool: str, confirm: bool = True, user: bool = False, **arguments):
    confirmer = ScriptedConfirmer(confirm)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer)
    return asyncio.run(ex.execute(ToolCall(tool, arguments), user_command=user)), confirmer


EDIT = [{"path": "app/tools/greet.py", "find": 'return "hi"', "replace": 'return "hello"'}]


def test_edits_are_exact_and_the_core_is_protected(world) -> None:
    source, _ = world
    edits, risk, facts = validate(source, EDIT)
    assert risk == "low" and facts["files"] == ["app/tools/greet.py"] and facts["changed_lines"] == 2
    for bad, code in (
        ([{"path": "app/security/permissions.py", "find": "2", "replace": "0"}], "protected"),
        ([{"path": "app/evolution/lab.py", "find": "", "replace": "x"}], "protected"),
        ([{"path": "../outside.py", "find": "", "replace": "x"}], "bad_path"),
        ([{"path": "C:/Windows/x.py", "find": "", "replace": "x"}], "bad_path"),
        ([{"path": "app/tools/greet.py", "find": "nope", "replace": "x"}], "edit_mismatch"),
        ([{"path": "app/tools/greet.py", "find": "", "replace": "x"}], "edit_mismatch"),
        ([{"path": "tests/test_greet.py", "find": "def test_b():\n    pass\n", "replace": ""}], "removes_tests"),
        ([{"path": "app/tools/run.exe", "find": "", "replace": "x"}], "bad_path"),
    ):
        with pytest.raises(ToolError) as caught:
            validate(source, bad)
        assert caught.value.code == code, bad


def test_windows_line_endings_are_kept(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    changes = apply_edits(tmp_path, [Edit("a.py", "x = 1\ny = 2", "x = 3\ny = 2")])
    assert changes["a.py"][1] == "x = 3\r\ny = 2\r\n"


def test_pytest_summary_is_read_exactly() -> None:
    run = lab.parse_pytest("...\n1 failed, 2285 passed, 18 skipped in 104.55s (0:01:44)")
    assert (run.passed, run.failed, run.skipped, run.green) == (2285, 1, 18, False)
    assert lab.parse_pytest("2 passed, 1 error in 1s").errors == 1
    assert not lab.parse_pytest("garbage").green


def test_improve_yourself_uses_real_failures_only(world) -> None:
    _, ctx = world
    result, _ = execute(ctx, "improve_yourself")
    assert result.result.ok and "nothing keeps failing" in result.result.message
    with ctx.database.transaction() as conn:
        for _ in range(4):
            conn.execute("INSERT INTO audit_log (ts, event, tool, details) VALUES (?, 'tool_result', ?, ?)",
                         (datetime.now(timezone.utc).isoformat(timespec="seconds"), "brightness_control",
                          json.dumps({"ok": False, "error": "unsupported", "message": "no DDC"})))
    found = analyze.failures(ctx.database)
    assert found[0]["tool"] == "brightness_control" and found[0]["count"] == 4
    result, _ = execute(ctx, "improve_yourself")
    assert "1 thing(s)" in result.result.message and "Nothing has changed" in result.result.message
    again, _ = execute(ctx, "improve_yourself")
    assert again.result.data["new"] == []  # the same finding isn't added twice


def test_the_whole_cycle_propose_test_apply_rollback(world, monkeypatch) -> None:
    source, ctx = world
    runner = Runner()
    monkeypatch.setattr(evolution_tools, "RUNNER", runner)
    proposed, _ = execute(ctx, "propose_code_change", title="Say hello", reason="hi is too short", edits=EDIT)
    assert proposed.result.ok and (source / "app/tools/greet.py").read_text() == ORIGINAL  # proposing changes nothing
    pid = proposed.result.data["id"]
    # applying before testing is refused
    early, _ = execute(ctx, "apply_proposal", id=pid)
    assert not early.result.ok and early.result.error == "not_tested"
    tested, asked = execute(ctx, "test_proposal", id=pid)
    assert tested.result.ok and asked.requests  # DANGEROUS: it asked
    copy = Path(ProposalStore(ctx.database).get(pid).report["copy"])
    assert 'return "hello"' in (copy / "app/tools/greet.py").read_text() and not (copy / ".venv").exists()
    assert (source / "app/tools/greet.py").read_text() == ORIGINAL  # only the copy changed
    assert all(cwd == copy for _, cwd in runner.calls) and not any("shell" in " ".join(a) for a, _ in runner.calls)
    declined, _ = execute(ctx, "apply_proposal", confirm=False, id=pid)
    assert not declined.result.ok and (source / "app/tools/greet.py").read_text() == ORIGINAL
    applied, asked = execute(ctx, "apply_proposal", user=True, id=pid)  # even your own command asks
    assert applied.result.ok and asked.requests and 'return "hello"' in (source / "app/tools/greet.py").read_text()
    assert "rebuild" in applied.result.message
    channels = deploy.load_channels(ctx.paths.data_dir / "evolution")
    assert channels["stable"]["applied"] == [pid] and channels["baseline"] == 12
    rolled, _ = execute(ctx, "rollback_proposal")
    assert rolled.result.ok and (source / "app/tools/greet.py").read_text() == ORIGINAL
    assert ProposalStore(ctx.database).get(pid).state == "rolled_back"


def test_a_failing_candidate_is_never_applied(world, monkeypatch) -> None:
    source, ctx = world
    monkeypatch.setattr(evolution_tools, "RUNNER", Runner("1 failed, 11 passed in 2s"))
    pid = execute(ctx, "propose_code_change", title="Say hello", reason="because", edits=EDIT)[0].result.data["id"]
    tested, _ = execute(ctx, "test_proposal", id=pid)
    assert not tested.result.ok and "did NOT pass" in tested.result.message
    applied, _ = execute(ctx, "apply_proposal", id=pid)
    assert not applied.result.ok and (source / "app/tools/greet.py").read_text() == ORIGINAL
    monkeypatch.setattr(evolution_tools, "RUNNER", Runner(lint=1))
    assert not execute(ctx, "test_proposal", id=pid)[0].result.ok  # linter problems fail it too


def test_fewer_passing_tests_than_before_fails(world, monkeypatch) -> None:
    _, ctx = world
    deploy.save_channels(ctx.paths.data_dir / "evolution", {"stable": {"applied": []}, "baseline": 20})
    monkeypatch.setattr(evolution_tools, "RUNNER", Runner("12 passed in 1s"))
    pid = execute(ctx, "propose_code_change", title="Say hello", reason="because", edits=EDIT)[0].result.data["id"]
    tested, _ = execute(ctx, "test_proposal", id=pid)
    assert not tested.result.ok and "20 passed before" in tested.result.message


def test_source_changed_after_the_test_needs_a_new_test(world, monkeypatch) -> None:
    source, ctx = world
    monkeypatch.setattr(evolution_tools, "RUNNER", Runner())
    pid = execute(ctx, "propose_code_change", title="Say hello", reason="because", edits=EDIT)[0].result.data["id"]
    execute(ctx, "test_proposal", id=pid)
    (source / "app/tools/greet.py").write_text(ORIGINAL + "# edited by hand\n")
    applied, _ = execute(ctx, "apply_proposal", id=pid)
    assert not applied.result.ok and applied.result.error == "stale_test"


def test_rollback_never_overwrites_a_later_edit(world, monkeypatch) -> None:
    source, ctx = world
    monkeypatch.setattr(evolution_tools, "RUNNER", Runner())
    pid = execute(ctx, "propose_code_change", title="Say hello", reason="because", edits=EDIT)[0].result.data["id"]
    execute(ctx, "test_proposal", id=pid)
    execute(ctx, "apply_proposal", id=pid)
    (source / "app/tools/greet.py").write_text("edited later\n")
    rolled, _ = execute(ctx, "rollback_proposal", id=pid)
    assert not rolled.result.ok and rolled.result.error == "changed_since"
    assert (source / "app/tools/greet.py").read_text() == "edited later\n"


def test_emergency_switch_stops_testing_and_applying(world, monkeypatch) -> None:
    _, ctx = world
    monkeypatch.setattr(evolution_tools, "RUNNER", Runner())
    pid = execute(ctx, "propose_code_change", title="Say hello", reason="because", edits=EDIT)[0].result.data["id"]
    emergency.STATE.set("evolution_disabled", True, by_voice=True)
    tested, asked = execute(ctx, "test_proposal", id=pid)
    assert not tested.result.ok and tested.result.error == "emergency" and not asked.requests
    assert not emergency.STATE.set("evolution_disabled", False, by_voice=True)  # voice can't turn it back on


def test_new_file_proposal_is_removed_again_on_rollback(world, monkeypatch) -> None:
    source, ctx = world
    monkeypatch.setattr(evolution_tools, "RUNNER", Runner())
    edits = [{"path": "app/tools/extra.py", "find": "", "replace": "X = 1\n"}]
    pid = execute(ctx, "propose_code_change", title="Add extra", reason="because", edits=edits)[0].result.data["id"]
    execute(ctx, "test_proposal", id=pid)
    assert execute(ctx, "apply_proposal", id=pid)[0].result.ok and (source / "app/tools/extra.py").exists()
    assert execute(ctx, "rollback_proposal", id=pid)[0].result.ok and not (source / "app/tools/extra.py").exists()


def test_no_source_folder_is_said_plainly(tmp_path: Path, monkeypatch) -> None:
    from app.utils import paths

    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    ctx = ToolContext(Settings(), paths=AppPaths.build(tmp_path))
    with pytest.raises(ToolError) as caught:
        evolution_tools.source_dir(ctx)
    assert caught.value.code == "no_source" and "JARVIS_SOURCE_DIR" in str(caught.value)


@pytest.mark.parametrize(("text", "tool", "arguments"), [
    ("improve yourself", "improve_yourself", {}),
    ("Jarvis, upgrade yourself", "improve_yourself", {}),
    ("show your proposals", "list_proposals", {}),
    ("test proposal 3", "test_proposal", {"id": 3}),
    ("apply proposal number 3", "apply_proposal", {"id": 3}),
    ("reject proposal #4", "reject_proposal", {"id": 4}),
    ("show proposal 2", "show_proposal", {"id": 2}),
    ("roll back proposal 3", "rollback_proposal", {"id": 3}),
    ("roll back the last update", "rollback_proposal", {}),
    ("open the evolution center", "jarvis_control", {"action": "evolution_center"}),
])
def test_evolution_shortcuts(text, tool, arguments) -> None:
    call = match_shortcut(text, lambda name: None)
    assert call is not None and (call.name, call.arguments) == (tool, arguments)


def test_code_proposals_after_untrusted_content_ask_first() -> None:
    from app.security.taint import PERSISTENT_TOOLS

    assert "propose_code_change" in PERSISTENT_TOOLS
    from app.core.missions import NOT_A_STEP

    assert {"test_proposal", "apply_proposal", "rollback_proposal"} <= NOT_A_STEP


def test_evolution_center_details_show_the_report(world, monkeypatch) -> None:
    from app.ui.evolution_center import details_text

    _, ctx = world
    monkeypatch.setattr(evolution_tools, "RUNNER", Runner())
    pid = execute(ctx, "propose_code_change", title="Say hello", reason="because", edits=EDIT)[0].result.data["id"]
    execute(ctx, "test_proposal", id=pid)
    text = details_text(ProposalStore(ctx.database).get(pid))
    assert "Test report: PASSED" in text and "12 passed" in text and 'return "hello"' in text
