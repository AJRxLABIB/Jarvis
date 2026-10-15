"""The evaluation lab: a proposal is tried on a COPY of Jarvis's source, with the project's own tests and linter.

The copy lives in data/evolution/candidates/<id> (never the real source). The commands are fixed argument
lists run through the reviewed CommandExecutor (no shell): ``python -m pytest`` and ``python -m ruff check``,
with the source's own virtual environment. A candidate passes only if nothing fails, no test errors, at least
as many tests pass as last time, and the linter is clean. The report keeps the real numbers and the tail of the
output - nothing is summarised by a model.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.evolution.proposals import Proposal, apply_edits
from app.tools.base import ToolError

SKIP_TOP = {".venv", "venv", "build", "dist", "data", "shots", "Claude outputs", ".git", "logs"}  # at the top only
SKIP_ANYWHERE = {".pytest_cache", ".ruff_cache", "__pycache__", "node_modules"}
SKIP_FILES = {".env"}
TEST_TIMEOUT, LINT_TIMEOUT = 600.0, 120.0
_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped)")


@dataclass(frozen=True)
class TestRun:
    passed: int
    failed: int
    errors: int
    skipped: int
    timed_out: bool
    output: str

    @property
    def green(self) -> bool:
        return not self.timed_out and self.failed == 0 and self.errors == 0 and self.passed > 0


def parse_pytest(output: str, timed_out: bool = False) -> TestRun:
    """Counts from pytest's last summary line ("2285 passed, 18 skipped in 104s")."""
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    summary = [line for line in output.splitlines() if re.search(r"\b(passed|failed|errors?)\b", line)
               and " in " in line]
    for number, word in _COUNTS.findall(summary[-1] if summary else ""):
        counts["errors" if word.startswith("error") else word] = int(number)
    return TestRun(counts["passed"], counts["failed"], counts["errors"], counts["skipped"], timed_out, output)


def python_for(source: Path) -> Path:
    """The source's own virtual environment (the one with the test tools), else this Python."""
    for candidate in (source / ".venv" / "Scripts" / "python.exe", source / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return candidate
    if getattr(sys, "frozen", False):
        raise ToolError(f"There's no .venv in {source} to run the tests with.", "no_python")
    return Path(sys.executable)


def make_copy(source: Path, target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)

    top = str(source)

    def ignore(folder: str, names: list[str]) -> set[str]:
        return {n for n in names if n in SKIP_ANYWHERE or n in SKIP_FILES or n.endswith((".pyc", ".log", ".db"))
                or (folder == top and n in SKIP_TOP)}

    shutil.copytree(source, target, ignore=ignore)
    return target


def write_changes(folder: Path, changes: dict[str, tuple[str | None, str]]) -> None:
    for path, (_old, new) in changes.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new.encode("utf-8"))  # exactly these bytes (no newline translation)


def evaluate(proposal: Proposal, source: Path, work: Path, executor: Any, baseline: int = 0) -> dict[str, Any]:
    """Copy, change, test, lint. Returns the report (``passed`` = may be applied)."""
    changes = apply_edits(source, proposal.edits)  # against the real source, as it is now
    folder = make_copy(source, work / str(proposal.id))
    write_changes(folder, changes)
    python = str(python_for(source))
    tests = executor.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider"], folder, timeout=TEST_TIMEOUT)
    run = parse_pytest(tests.output, tests.timed_out)
    lint = executor.run([python, "-m", "ruff", "check", "app", "tests"], folder, timeout=LINT_TIMEOUT)
    lint_ok = lint.returncode == 0 and not lint.timed_out
    reasons = []
    if run.timed_out:
        reasons.append("the tests took too long")
    if run.failed or run.errors:
        reasons.append(f"{run.failed} test(s) failed, {run.errors} error(s)")
    if run.passed == 0 and not run.timed_out:
        reasons.append("no tests ran")
    if baseline and run.passed < baseline:
        reasons.append(f"only {run.passed} tests passed; {baseline} passed before")
    if not lint_ok:
        reasons.append("the linter found problems")
    return {
        "passed": not reasons, "reasons": reasons, "tests": {"passed": run.passed, "failed": run.failed,
                                                             "errors": run.errors, "skipped": run.skipped},
        "baseline": baseline, "lint_ok": lint_ok, "test_output": run.output[-1500:],
        "lint_output": (lint.output or "")[-800:], "copy": str(folder),
    }
