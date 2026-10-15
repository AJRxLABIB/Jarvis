"""Reviewing a change before it is committed: what changed, and what looks risky about it.

``analyse_diff`` reads a unified diff (``git diff``) and reports per file how many lines were added and removed,
plus flags a careful reviewer would raise:

* tests removed or weakened (a test file loses more test functions than it gains, or a test is skipped),
* a secret added (a key, token, password or private key in an added line - the text itself is never repeated),
* a very large deletion,
* a file that looks like it holds secrets (.env, *.pem, ...) is part of the change.

It is deterministic and read-only. A clean review is not proof the change is correct: run the tests too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.utils.redaction import REDACTED, redact

LARGE_DELETION = 200
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_\w+|^\s*(?:it|test|describe)\s*\(|^\s*@Test\b|^\s*public\s+function\s+test\w+", re.M)
_SKIP = re.compile(r"@pytest\.mark\.skip|@unittest\.skip|\bxit\s*\(|\bxdescribe\s*\(|\bit\.skip\s*\(|\btest\.skip\s*\(|"
                   r"markTestSkipped|@Ignore\b", re.I)
_SECRET_FILE = re.compile(r"(^|/)(\.env(\.(local|production|development))?|id_rsa|id_ed25519|credentials(\.json)?|"
                          r"secrets\.(json|ya?ml)|\.npmrc|\.pypirc|\.netrc)$|\.(pem|key|p12|pfx|jks|keystore|kdbx)$", re.I)


@dataclass
class FileChange:
    path: str
    added: int = 0
    removed: int = 0
    is_test: bool = False
    tests_added: int = 0
    tests_removed: int = 0
    skips_added: int = 0
    secret_lines: int = 0
    new_file: bool = False
    deleted_file: bool = False


@dataclass
class Review:
    files: list[FileChange] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)

    @property
    def clean(self) -> bool:
        return not self.flags


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    low = f"/{path.lower()}"
    return ("/tests/" in low or "/test/" in low or "/__tests__/" in low or name.startswith("test_")
            or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts", "test.php", "test.java")))


def _holds_secret(line: str) -> bool:
    """True when redaction would change the line, i.e. it contains a secret-looking value. The value isn't kept."""
    return REDACTED in redact(line)


def analyse_diff(diff: str) -> Review:
    review = Review()
    current: FileChange | None = None
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", raw)
            path = match.group(2) if match else raw[11:]
            current = FileChange(path, is_test=_is_test_path(path))
            review.files.append(current)
        elif current is None:
            continue
        elif raw.startswith("new file mode"):
            current.new_file = True
        elif raw.startswith("deleted file mode"):
            current.deleted_file = True
        elif raw.startswith("+++") or raw.startswith("---"):
            continue
        elif raw.startswith("+"):
            current.added += 1
            body = raw[1:]
            if _TEST_DEF.search(body):
                current.tests_added += 1
            if _SKIP.search(body):
                current.skips_added += 1
            if _holds_secret(body):
                current.secret_lines += 1
        elif raw.startswith("-"):
            current.removed += 1
            if _TEST_DEF.search(raw[1:]):
                current.tests_removed += 1
    for f in review.files:
        if f.is_test and f.tests_removed > f.tests_added:
            review.flags.append(f"tests removed in {f.path}: {f.tests_removed} removed, {f.tests_added} added")
        if f.skips_added:
            review.flags.append(f"{f.skips_added} test(s) skipped in {f.path}")
        if f.deleted_file and f.is_test:
            review.flags.append(f"the test file {f.path} was deleted")
        if f.secret_lines:
            review.flags.append(f"{f.secret_lines} added line(s) in {f.path} look like a secret (key, token or password)")
        if f.removed >= LARGE_DELETION and f.removed > 3 * max(f.added, 1):
            review.flags.append(f"{f.path} loses {f.removed} lines and gains {f.added}")
        if _SECRET_FILE.search(f.path):
            review.flags.append(f"{f.path} is the kind of file that holds secrets and should not be committed")
    return review


def describe(review: Review) -> str:
    if not review.files:
        return "There are no changes to review."
    names = ", ".join(f"{f.path} (+{f.added}/-{f.removed})" for f in review.files[:8])
    more = f" and {len(review.files) - 8} more" if len(review.files) > 8 else ""
    head = f"{len(review.files)} file(s) changed, +{review.added}/-{review.removed}: {names}{more}."
    if review.clean:
        return head + " Nothing risky stands out (that is not proof it is correct: run the tests)."
    return head + " Look at this before committing: " + "; ".join(review.flags) + "."
