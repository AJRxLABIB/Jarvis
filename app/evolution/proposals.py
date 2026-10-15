"""Proposals: what could be better, why (evidence), the exact change, its risk, its test report, its state."""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from app.tools.base import ToolError

if TYPE_CHECKING:
    from app.memory.database import Database

STATES = ("proposed", "testing", "tested", "failed", "applied", "rejected", "rolled_back")
OPEN_STATES = ("proposed", "tested", "failed")

# Never changed by the evolution engine (a human edits these, by hand, with a review).
PROTECTED = (
    "app/security/", "app/evolution/", "app/remote/", "app/agent/tool_manager.py", "app/core/emergency.py",
    "app/config/settings.py", "app/config/files.py", "config/permissions.json", "config/mcp.example.json",
    "tests/test_security_static.py", "tests/conftest.py", "packaging/", ".env", "run.py", "pyproject.toml",
    # trust boundaries of the task engine: who may do what, which tools can be mission steps, the lifecycle rules,
    # the schema migrations and the git secret filter
    "app/core/agents.py", "app/core/orchestrator.py", "app/core/missions.py", "app/core/taskstate.py",
    "app/memory/database.py", "app/tools/developer.py",
)
ALLOWED_SUFFIXES = (".py", ".json", ".md", ".txt", ".yaml", ".yml", ".qss")
MAX_FILES, MAX_CHANGED_LINES, MAX_EDIT_CHARS = 5, 200, 20000


@dataclass
class Edit:
    path: str  # relative to the source folder, forward slashes
    find: str
    replace: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "find": self.find, "replace": self.replace}


@dataclass
class Proposal:
    id: int
    created_at: str
    kind: str
    title: str
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    risk: str = "low"
    state: str = "proposed"
    edits: list[Edit] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    applied_at: str | None = None
    backup: str = ""
    fingerprint: str = ""

    def summary(self) -> str:
        what = f"{len(self.edits)} edit(s) in {len({e.path for e in self.edits})} file(s)" if self.edits else "idea"
        return f"#{self.id} [{self.state}] {self.title} ({what}, risk {self.risk})"


def clean_path(path: str) -> str:
    text = path.strip().replace("\\", "/")
    posix = PurePosixPath(text)
    if not text or posix.is_absolute() or ":" in text or ".." in posix.parts:
        raise ToolError(f"'{path}' must be a path inside Jarvis's source folder (like app/tools/windows.py).",
                        "bad_path")
    return str(posix)


def check_allowed(path: str) -> None:
    if any(path == p.rstrip("/") or path.startswith(p) for p in PROTECTED):
        raise ToolError(f"{path} is part of Jarvis's security core: it can't be changed by a proposal.", "protected")
    if not path.endswith(ALLOWED_SUFFIXES):
        raise ToolError(f"{path}: only source, text and config files can be changed.", "bad_path")


def _test_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().startswith(("def test_", "async def test_")))


def apply_edits(source: Path, edits: list[Edit]) -> dict[str, tuple[str | None, str]]:
    """{path: (old text exactly as on disk or None for a new file, new text as it will be written)}. Each
    ``find`` must occur exactly once (an empty find creates a new file). Edits work on \\n line endings; a file
    that uses \\r\\n keeps them. Nothing is written here."""
    work: dict[str, list] = {}  # path -> [old raw, current (\\n), crlf]
    for edit in edits:
        target = source / edit.path
        if edit.path not in work:
            if target.is_file():
                raw = target.read_bytes().decode("utf-8")
                work[edit.path] = [raw, raw.replace("\r\n", "\n"), "\r\n" in raw]
            elif edit.find:
                raise ToolError(f"{edit.path} doesn't exist.", "not_found")
            else:
                work[edit.path] = [None, None, False]
        entry = work[edit.path]
        find = edit.find.replace("\r\n", "\n")
        replace = edit.replace.replace("\r\n", "\n")
        if find:
            if entry[1] is None:
                raise ToolError(f"{edit.path} doesn't exist yet.", "not_found")
            count = entry[1].count(find)
            if count != 1:
                raise ToolError(f"In {edit.path} the text to replace was found {count} times (it must be exactly "
                                "once).", "edit_mismatch")
            entry[1] = entry[1].replace(find, replace, 1)
        elif entry[1] is not None:
            raise ToolError(f"{edit.path} already exists: say which text to replace.", "edit_mismatch")
        else:
            entry[1] = replace
    return {path: (old, current.replace("\n", "\r\n") if crlf else current)
            for path, (old, current, crlf) in work.items()}


def validate(source: Path, raw_edits: list[dict[str, Any]]) -> tuple[list[Edit], str, dict[str, Any]]:
    """Checked edits, a risk estimate and the facts behind it. Raises ToolError for anything not allowed."""
    if not raw_edits:
        raise ToolError("A code proposal needs at least one edit.", "no_edits")
    edits = []
    for raw in raw_edits:
        path = clean_path(str(raw.get("path", "")))
        check_allowed(path)
        find, replace = str(raw.get("find", "")), str(raw.get("replace", ""))
        if len(find) + len(replace) > MAX_EDIT_CHARS:
            raise ToolError(f"The edit to {path} is too big for one proposal.", "too_big")
        edits.append(Edit(path, find, replace))
    files = {e.path for e in edits}
    if len(files) > MAX_FILES:
        raise ToolError(f"A proposal may change at most {MAX_FILES} files.", "too_big")
    changes = apply_edits(source, edits)
    changed = 0
    for path, (old, new) in changes.items():
        diff = difflib.unified_diff((old or "").splitlines(), new.splitlines(), lineterm="", n=0)
        changed += sum(1 for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---")))
        if path.startswith("tests/") and old is not None and _test_count(new) < _test_count(old):
            raise ToolError(f"The change removes tests from {path}: not allowed.", "removes_tests")
    if changed > MAX_CHANGED_LINES:
        raise ToolError(f"The change touches {changed} lines; at most {MAX_CHANGED_LINES} per proposal.", "too_big")
    core = any(p.startswith(("app/agent/", "app/core/", "app/memory/", "app/ai/")) for p in files)
    risk = "high" if core or changed > 80 or len(files) > 3 else "medium" if changed > 20 or len(files) > 1 else "low"
    return edits, risk, {"files": sorted(files), "changed_lines": changed, "core": core}


def diff_text(source: Path, edits: list[Edit]) -> str:
    parts = []
    for path, (old, new) in apply_edits(source, edits).items():
        parts.extend(difflib.unified_diff((old or "").splitlines(), new.splitlines(), f"a/{path}", f"b/{path}",
                                          lineterm=""))
    return "\n".join(parts)


def sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


def fingerprint(source: Path, paths: list[str]) -> str:
    """The current content of the files a proposal touches: tested == applied needs the same source."""
    parts = []
    for path in sorted(set(paths)):
        target = source / path
        parts.append(f"{path}:{sha(target.read_bytes()) if target.is_file() else '-'}")
    return sha("|".join(parts))


class ProposalStore:
    def __init__(self, database: "Database") -> None:
        self._db = database

    @staticmethod
    def _row(row) -> Proposal:
        return Proposal(
            id=row[0], created_at=row[1], kind=row[2], title=row[3], reason=row[4],
            evidence=json.loads(row[5] or "{}"), risk=row[6], state=row[7],
            edits=[Edit(**e) for e in json.loads(row[8] or "[]")], report=json.loads(row[9] or "{}"),
            applied_at=row[10], backup=row[11], fingerprint=row[12],
        )

    _COLUMNS = "id, created_at, kind, title, reason, evidence, risk, state, edits, report, applied_at, backup, fingerprint"

    def add(self, kind: str, title: str, reason: str = "", evidence: dict | None = None, risk: str = "low",
            edits: list[Edit] | None = None, *, now: str) -> Proposal:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO evolution_proposals (created_at, kind, title, reason, evidence, risk, state, edits) "
                "VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)",
                (now, kind, title[:200], reason[:2000], json.dumps(evidence or {}), risk,
                 json.dumps([e.to_dict() for e in edits or []])))
            new_id = cursor.lastrowid
        return self.get(new_id)

    def get(self, proposal_id: int) -> Proposal:
        with self._db.connect() as conn:
            row = conn.execute(f"SELECT {self._COLUMNS} FROM evolution_proposals WHERE id = ?",
                               (proposal_id,)).fetchone()
        if row is None:
            raise ToolError(f"There's no proposal #{proposal_id}.", "not_found")
        return self._row(row)

    def list(self, states: tuple[str, ...] | None = None, limit: int = 50) -> list[Proposal]:
        sql = f"SELECT {self._COLUMNS} FROM evolution_proposals"
        params: tuple = ()
        if states:
            sql += f" WHERE state IN ({', '.join('?' * len(states))})"
            params = states
        with self._db.connect() as conn:
            rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [self._row(r) for r in rows]

    def update(self, proposal_id: int, **fields: Any) -> Proposal:
        allowed = {"state", "report", "applied_at", "backup", "fingerprint", "risk"}
        if not fields or set(fields) - allowed:
            raise ValueError(fields)
        if "state" in fields and fields["state"] not in STATES:
            raise ValueError(fields["state"])
        values = [json.dumps(v) if k == "report" else v for k, v in fields.items()]
        with self._db.transaction() as conn:
            conn.execute(f"UPDATE evolution_proposals SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         (*values, proposal_id))
        return self.get(proposal_id)

    def has_open_insight(self, key: str) -> bool:
        return any(p.evidence.get("key") == key for p in self.list(("proposed",), limit=200) if p.kind == "insight")
