"""3.24: computer-agent before/after verification, and the developer pipeline (test-gated edits that undo
themselves on regression - including tests weakened to pass)."""

from __future__ import annotations

from pathlib import Path


from app.core import dev_pipeline as dp
from app.core import screen_verify as sv


# ------------------------------------------------------------------ screen verification


class FakeThumb:
    """Stands in for a small grey image: verify() only compares via pixels_changed, which we monkeypatch."""

    def __init__(self, tag: str) -> None:
        self.tag = tag


def test_pixels_changed_needs_both(monkeypatch) -> None:
    assert sv.pixels_changed(None, FakeThumb("a")) is None
    assert sv.pixels_changed(FakeThumb("a"), None) is None


def test_verify_confirms_when_screen_changes(monkeypatch) -> None:
    monkeypatch.setattr(sv, "pixels_changed", lambda a, b, threshold=sv.CHANGE_THRESHOLD: True)
    before = sv.Snapshot(thumb=FakeThumb("a"), foreground="Notepad")
    after = sv.Snapshot(thumb=FakeThumb("b"), foreground="Notepad")
    v = sv.verify(before, after)
    assert v.ok is True and v.note == ""


def test_verify_reports_no_change(monkeypatch) -> None:
    monkeypatch.setattr(sv, "pixels_changed", lambda a, b, threshold=sv.CHANGE_THRESHOLD: False)
    before = sv.Snapshot(thumb=FakeThumb("a"), foreground="Notepad")
    after = sv.Snapshot(thumb=FakeThumb("a"), foreground="Notepad")
    v = sv.verify(before, after)
    assert v.ok is False and "may not have worked" in v.note


def test_verify_none_when_cannot_capture() -> None:
    v = sv.verify(sv.Snapshot(thumb=None), sv.Snapshot(thumb=None))
    assert v.ok is None and "couldn't" in v.detail


def test_foreground_change_counts_as_effect(monkeypatch) -> None:
    monkeypatch.setattr(sv, "pixels_changed", lambda a, b, threshold=sv.CHANGE_THRESHOLD: False)
    before = sv.Snapshot(thumb=FakeThumb("a"), foreground="Notepad")
    after = sv.Snapshot(thumb=FakeThumb("a"), foreground="Chrome")
    v = sv.verify(before, after, expect_foreground_change=True)
    assert v.ok is True and "Chrome" in v.detail


def test_typed_text_appearing_confirms() -> None:
    before = sv.Snapshot(ocr_text="")
    after = sv.Snapshot(ocr_text="dear team, the report is ready")
    v = sv.verify(before, after, expect_text="Dear team")
    assert v.ok is True and "appeared" in v.detail


def test_text_appeared_is_forgiving_but_not_blind() -> None:
    assert sv.text_appeared("here is the search box with query", "search")
    assert not sv.text_appeared("totally different screen", "invoice number 4471")


# ------------------------------------------------------------------ developer pipeline (pure)


def test_parse_pytest_and_jest() -> None:
    assert dp.parse_tests("5 passed, 1 failed in 0.3s", ok=False) == dp.TestSummary(5, 1, True, False)
    assert dp.parse_tests("Tests: 1 failed, 5 passed, 6 total", ok=False).failed == 1
    assert dp.parse_tests("2 passed, 1 error in 0.1s", ok=False).failed == 1  # errors count as failures
    assert not dp.parse_tests("built ok", ok=True).ran


def test_regression_detection() -> None:
    base = dp.parse_tests("5 passed, 0 failed", ok=True)
    assert dp.regressed(base, dp.parse_tests("6 passed, 0 failed", ok=True))[0] is False
    assert dp.regressed(base, dp.parse_tests("4 passed, 1 failed", ok=False))[0] is True
    # the anti-cheat: making the suite green by removing tests is a regression
    weakened = dp.parse_tests("3 passed, 0 failed", ok=True)
    bad, why = dp.regressed(base, weakened)
    assert bad and "weakened" in why


# ------------------------------------------------------------------ developer pipeline (the tool)


class _Result:
    def __init__(self, ok, output=""):
        self.ok = ok
        self.data = {"output": output}


def _project(tmp_path: Path):
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.security.validator import PathPolicy
    from app.tools.base import ToolContext

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    known = {"HOME": tmp_path, "DESKTOP": tmp_path, "DOWNLOADS": tmp_path, "DOCUMENTS": tmp_path}
    policy = PathPolicy.build(PermissionsConfig(safe_roots=["{HOME}"]), known=known)
    from types import SimpleNamespace

    paths = SimpleNamespace(data_dir=tmp_path / "data")
    (tmp_path / "data").mkdir()
    ctx = ToolContext(Settings(), paths=paths)
    ctx.path_policy = policy
    return root, ctx


def test_safe_edit_keeps_when_tests_pass(tmp_path: Path, monkeypatch) -> None:
    from app.tools import code_tools

    root, ctx = _project(tmp_path)
    monkeypatch.setattr("app.core.projects.known_project_folders", lambda c: [root])
    monkeypatch.setattr(code_tools, "_check_command", lambda a, c: __import__("types").SimpleNamespace(command="pytest"))
    runs = iter([_Result(True, "5 passed in 1s"), _Result(True, "5 passed in 1s")])
    monkeypatch.setattr("app.tools.developer.run_command", lambda cmd, ctx: next(runs))

    args = code_tools.SafeEditArgs(project=str(root), changes=[
        code_tools.SafeEditFile(path=str(root / "src" / "app.py"),
                                edits=[code_tools.Edit(find="VALUE = 1", replace="VALUE = 2")])])
    result = code_tools.safe_edit(args, ctx)
    assert result.ok, result.message
    assert (root / "src" / "app.py").read_text() == "VALUE = 2\n"


def test_safe_edit_reverts_when_tests_break(tmp_path: Path, monkeypatch) -> None:
    from app.tools import code_tools

    root, ctx = _project(tmp_path)
    monkeypatch.setattr("app.core.projects.known_project_folders", lambda c: [root])
    monkeypatch.setattr(code_tools, "_check_command", lambda a, c: __import__("types").SimpleNamespace(command="pytest"))
    runs = iter([_Result(True, "5 passed in 1s"), _Result(False, "4 passed, 1 failed in 1s")])
    monkeypatch.setattr("app.tools.developer.run_command", lambda cmd, ctx: next(runs))

    args = code_tools.SafeEditArgs(project=str(root), changes=[
        code_tools.SafeEditFile(path=str(root / "src" / "app.py"),
                                edits=[code_tools.Edit(find="VALUE = 1", replace="VALUE = BROKEN")])])
    result = code_tools.safe_edit(args, ctx)
    assert not result.ok and result.error == "reverted"
    assert (root / "src" / "app.py").read_text() == "VALUE = 1\n"  # rolled back to the original


def test_safe_edit_reverts_when_tests_are_weakened(tmp_path: Path, monkeypatch) -> None:
    from app.tools import code_tools

    root, ctx = _project(tmp_path)
    monkeypatch.setattr("app.core.projects.known_project_folders", lambda c: [root])
    monkeypatch.setattr(code_tools, "_check_command", lambda a, c: __import__("types").SimpleNamespace(command="pytest"))
    runs = iter([_Result(True, "5 passed in 1s"), _Result(True, "3 passed in 1s")])  # 2 tests vanished
    monkeypatch.setattr("app.tools.developer.run_command", lambda cmd, ctx: next(runs))

    args = code_tools.SafeEditArgs(project=str(root), changes=[
        code_tools.SafeEditFile(path=str(root / "src" / "app.py"),
                                edits=[code_tools.Edit(find="VALUE = 1", replace="VALUE = 2")])])
    result = code_tools.safe_edit(args, ctx)
    assert not result.ok and result.error == "reverted"
    assert (root / "src" / "app.py").read_text() == "VALUE = 1\n"
