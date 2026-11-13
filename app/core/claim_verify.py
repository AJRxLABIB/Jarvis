"""Independent verification of "it worked" claims: look at the real file / repository instead of trusting the agent's own message.

* ``check_file``  - "I saved report.docx": does the file exist, is it non-empty and recent enough, does it contain the expected text, and can it be
                    REOPENED as its own format (docx / xlsx / pdf / json / zip are opened for real; plain text is decoded).
* ``check_git``   - "I committed / switched branch": inspect the actual repository - current branch, latest commit message, whether the tree is clean,
                    and which files the last commit touched against an expected set (unintended changes are reported, not hidden).

Both are read-only and pure over their inputs (a path; an injected ``run(argv, cwd) -> (returncode, output)`` with the reviewed git argument lists),
so they are tested without a real repository or Office. Each returns a ``Verdict``: ``verified`` (every check passed), ``failed`` (a check failed),
or ``unverified`` (the evidence could not be read - which is NOT success). Contents are never returned, only pass/fail facts and short counts.
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

Run = Callable[[list[str], Path], tuple[int, str]]
MAX_READ = 5_000_000


@dataclass
class Verdict:
    status: str                                   # verified | failed | unverified
    checks: list[tuple[str, bool, str]] = field(default_factory=list)   # (what, passed, detail)

    @property
    def ok(self) -> bool:
        return self.status == "verified"

    def text(self) -> str:
        head = {"verified": "VERIFIED", "failed": "NOT VERIFIED - a check failed", "unverified": "UNVERIFIED - the evidence could not be read"}[self.status]
        return head + "\n" + "\n".join(f"  [{'pass' if ok else 'FAIL'}] {what}: {detail}" for what, ok, detail in self.checks)


def _done(checks: list[tuple[str, bool, str]], unreadable: bool = False) -> Verdict:
    if unreadable:
        return Verdict("unverified", checks)
    return Verdict("verified" if all(ok for _, ok, _ in checks) else "failed", checks)


def _text_of(path: Path) -> str | None:
    """Text of the file in its real format, or None when it cannot be opened as that format."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".docx", ".xlsx", ".pptx"):
            with zipfile.ZipFile(path) as z:
                if z.testzip() is not None:
                    return None
                names = [n for n in z.namelist() if n.endswith(".xml") and ("word/" in n or "xl/" in n or "ppt/" in n)]
                return " ".join(z.read(n).decode("utf-8", "ignore") for n in names[:60])[:MAX_READ]
        if suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                return " ".join(z.namelist()) if z.testzip() is None else None
        if suffix == ".pdf":
            data = path.read_bytes()[:MAX_READ]
            return data.decode("latin-1") if data.startswith(b"%PDF") else None
        raw = path.read_bytes()[:MAX_READ]
        text = raw.decode("utf-8")
        if suffix == ".json":
            json.loads(text)
        return text
    except (OSError, ValueError, zipfile.BadZipFile, UnicodeDecodeError):
        return None


def check_file(path: Path, contains: list[str] | None = None, min_bytes: int = 1, max_age_s: float | None = None, now: float | None = None) -> Verdict:
    checks: list[tuple[str, bool, str]] = []
    try:
        st = path.stat()
    except OSError:
        return _done([("file exists", False, f"{path.name} was not found")])
    checks.append(("file exists", path.is_file(), path.name))
    checks.append(("not empty", st.st_size >= min_bytes, f"{st.st_size} bytes (at least {min_bytes})"))
    if max_age_s is not None:
        age = (now if now is not None else time.time()) - st.st_mtime
        checks.append(("written recently", age <= max_age_s, f"modified {int(age)} s ago (limit {int(max_age_s)} s)"))
    text = _text_of(path)
    checks.append(("can be reopened as " + (path.suffix.lower().lstrip(".") or "text"), text is not None, "opened and read" if text is not None else "could not be opened - it may be corrupt"))
    if contains:
        if text is None:
            return _done(checks + [("expected text present", False, "not checked: the file could not be read")], unreadable=True)
        low = text.lower()
        missing = [c for c in contains if c.lower() not in low]
        checks.append(("expected text present", not missing, f"{len(contains) - len(missing)} of {len(contains)} found" + (f"; missing: {', '.join(m[:30] for m in missing[:3])}" if missing else "")))
    return _done(checks)


def check_git(folder: Path, run: Run, branch: str = "", message_contains: str = "", expect_files: list[str] | None = None, expect_clean: bool | None = None) -> Verdict:
    checks: list[tuple[str, bool, str]] = []

    def ask(*argv: str) -> str | None:
        try:
            rc, out = run(["git", *argv], folder)
        except Exception:
            return None
        return out.strip() if rc == 0 else None

    current = ask("rev-parse", "--abbrev-ref", "HEAD")
    if current is None:
        return _done([("is a git repository", False, "git could not read this folder")], unreadable=True)
    checks.append(("is a git repository", True, folder.name))
    if branch:
        checks.append(("on the expected branch", current == branch, f"on '{current}', expected '{branch}'"))
    last = ask("log", "-n1", "--pretty=%h %s")
    if message_contains:
        checks.append(("latest commit message", last is not None and message_contains.lower() in last.lower(), f"latest: {last or 'none'}"))
    if expect_files is not None:
        out = ask("show", "--name-only", "--pretty=format:", "HEAD")
        touched = sorted({line.strip() for line in (out or "").splitlines() if line.strip()})
        wanted = {f.strip().replace("\\", "/") for f in expect_files if f.strip()}
        extra = [t for t in touched if t not in wanted]
        missing = sorted(wanted - set(touched))
        checks.append(("last commit touched exactly the expected files", out is not None and not extra and not missing,
                       f"{len(touched)} file(s)" + (f"; unexpected: {', '.join(extra[:4])}" if extra else "") + (f"; not in commit: {', '.join(missing[:4])}" if missing else "")))
    if expect_clean is not None:
        status = ask("status", "--short")
        dirty = [x for x in (status or "").splitlines() if x.strip()]
        checks.append(("working tree " + ("clean" if expect_clean else "has changes"), status is not None and (not dirty) == expect_clean, f"{len(dirty)} changed file(s)"))
    return _done(checks)
