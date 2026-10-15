"""The developer pipeline: guard code edits with the project's own tests, and undo them automatically if they
break or weaken the tests.

The coder agent (or you) proposes edits to a project. The pipeline:
  1. runs the project's test command first, to get a baseline;
  2. applies the edits (each backed up);
  3. runs the tests again;
  4. keeps the change only if the tests still pass AND no test was lost - otherwise it rolls every edit back.

"No test was lost" is the anti-cheat: a change that makes the suite pass by deleting or skipping tests counts as a
regression, not a success. The test output is untrusted data (it may contain attacker-controlled text); only the
pass/fail numbers are used, never anything in it as an instruction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# pytest: "5 passed, 1 failed, 2 skipped in 0.3s"; jest/vitest: "Tests: 1 failed, 5 passed, 6 total"
_PASSED = re.compile(r"(\d+)\s+passed", re.I)
_FAILED = re.compile(r"(\d+)\s+failed", re.I)
_ERRORS = re.compile(r"(\d+)\s+error", re.I)


@dataclass(frozen=True)
class TestSummary:
    passed: int
    failed: int
    ran: bool          # did we actually get numbers out of the run?
    ok: bool           # the command exited successfully

    @property
    def total(self) -> int:
        return self.passed + self.failed

    def line(self) -> str:
        if not self.ran:
            return "no test counts found" + ("" if self.ok else " (command failed)")
        return f"{self.passed} passed, {self.failed} failed"


def parse_tests(output: str, *, ok: bool) -> TestSummary:
    """Pull pass/fail counts out of a test run's output. ``ok`` is whether the command exited zero."""
    text = output or ""
    p = _PASSED.search(text)
    f = _FAILED.search(text)
    e = _ERRORS.search(text)
    passed = int(p.group(1)) if p else 0
    failed = (int(f.group(1)) if f else 0) + (int(e.group(1)) if e else 0)
    ran = bool(p or f or e)
    return TestSummary(passed, failed, ran, ok)


def regressed(before: TestSummary, after: TestSummary) -> tuple[bool, str]:
    """(did the edit make things worse, why). Worse = the suite now fails, a test newly fails, or fewer tests pass
    than before the edit (tests were removed, skipped or renamed away)."""
    if not after.ok:
        return True, "the tests no longer pass (the command failed)"
    if after.failed > before.failed:
        return True, f"more tests fail now ({after.failed} vs {before.failed})"
    if before.ran and after.ran and after.passed < before.passed:
        return True, f"fewer tests pass now ({after.passed} vs {before.passed}) - tests must not be weakened to pass"
    if before.ran and not after.ran:
        return True, "the tests no longer report results after the change"
    return False, ""


def verdict_message(before: TestSummary, after: TestSummary) -> tuple[bool, str]:
    """(keep the change, human message)."""
    bad, why = regressed(before, after)
    if bad:
        return False, f"Reverted: {why}. Before: {before.line()}; after: {after.line()}. Your code is back as it was."
    kept = "Applied and verified" if after.ran else "Applied"
    return True, f"{kept}: tests still pass ({after.line()}; baseline {before.line()})."
