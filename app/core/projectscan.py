"""A security look at a project you are working on (read-only, no network, no commands).

Finds the common, mechanical mistakes: secrets written into code, risky calls (eval/exec, shell=True, unsafe
deserialization, TLS checks turned off), a secret file that git would commit, and unpinned dependencies. It is a
checklist, not an audit - a clean result means "none of these", not "secure". Findings give file and line, never the
secret itself. Test files are skipped for the risky-call checks (tests often do these things on purpose).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.core.codeindex import _read, _rel
from app.core.diffreview import _SECRET_FILE, _holds_secret, _is_test_path
from app.core.projects import code_files

MAX_FINDINGS = 40

_RISKY: dict[str, tuple[tuple[re.Pattern[str], str, str], ...]] = {
    ".py": (
        (re.compile(r"(?<![\w.])eval\("), "high", "eval() runs text as code"),
        (re.compile(r"(?<![\w.])exec\("), "high", "exec() runs text as code"),
        (re.compile(r"shell\s*=\s*True"), "high", "shell=True lets text become shell commands"),
        (re.compile(r"\bos\.system\("), "medium", "os.system() runs a shell command"),
        (re.compile(r"\bpickle\.loads?\("), "high", "pickle can run code when loading untrusted data"),
        (re.compile(r"\byaml\.load\((?!.*Loader)"), "high", "yaml.load without a safe Loader"),
        (re.compile(r"verify\s*=\s*False"), "high", "TLS certificate checking is turned off"),
        (re.compile(r"\bhashlib\.(md5|sha1)\("), "low", "md5/sha1 are weak for passwords or signatures"),
    ),
    ".js": (
        (re.compile(r"(?<![\w.])eval\("), "high", "eval() runs text as code"),
        (re.compile(r"\.innerHTML\s*="), "medium", "innerHTML with untrusted text allows script injection"),
        (re.compile(r"document\.write\("), "medium", "document.write can inject script"),
        (re.compile(r"rejectUnauthorized\s*:\s*false"), "high", "TLS certificate checking is turned off"),
    ),
    ".php": (
        (re.compile(r"(?<![\w>])eval\("), "high", "eval() runs text as code"),
        (re.compile(r"\bmysql_query\("), "high", "removed mysql_* API (and usually unsafe SQL)"),
        (re.compile(r"(?:SELECT|INSERT|UPDATE|DELETE)[^;\n]*['\"]\s*\.\s*\$_(?:GET|POST|REQUEST)", re.I), "high",
         "SQL built from request input (injection)"),
    ),
}
_RISKY[".ts"] = _RISKY[".jsx"] = _RISKY[".tsx"] = _RISKY[".js"]


@dataclass(frozen=True)
class Finding:
    severity: str  # high | medium | low
    file: str
    line: int
    message: str

    def line_text(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"[{self.severity}] {where} - {self.message}"


def _ignored_by_git(root: Path, name: str) -> bool:
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False
    wanted = {name, "/" + name, name + "*", ".env*", "*.env", "/" + name + "/"}
    return any(line.strip() in wanted for line in lines)


def _requirements(root: Path) -> list[Finding]:
    findings = []
    for path in root.glob("requirements*.txt"):
        text = _read(path) or ""
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith(("-", "git+", "http")):
                continue
            if not re.search(r"[<>=!~]=?|@", line):
                findings.append(Finding("low", path.name, number, f"'{line}' has no version limit (a future release "
                                                                  "could change or be tampered with)"))
    return findings


def scan(root: Path) -> list[Finding]:
    root = Path(root)
    findings: list[Finding] = []
    git_repo = (root / ".git").exists()
    for secret_name in (".env", ".env.local", ".env.production"):
        if (root / secret_name).is_file() and git_repo and not _ignored_by_git(root, secret_name):
            findings.append(Finding("high", secret_name, 0, "a secret file that .gitignore does not exclude "
                                                            "(git would commit it)"))
    for path in code_files(root, 3000):
        rel = _rel(path, root)
        text = _read(path)
        if text is None:
            continue
        is_test = _is_test_path(rel)
        rules = () if is_test else _RISKY.get(path.suffix.lower(), ())
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//", "*", "/*")):
                continue
            if not is_test and _holds_secret(line):
                findings.append(Finding("high", rel, number, "looks like a secret written into the code "
                                                             "(move it to .env and rotate it)"))
            for pattern, severity, message in rules:
                if pattern.search(line):
                    findings.append(Finding(severity, rel, number, message))
        if _SECRET_FILE.search(rel) and not is_test:
            findings.append(Finding("medium", rel, 0, "a file that usually holds secrets is inside the project"))
    findings += _requirements(root)
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda f: (order[f.severity], f.file, f.line))


def describe(findings: list[Finding]) -> str:
    if not findings:
        return ("None of the common problems were found (secrets in code, eval/exec, shell=True, unsafe loading, "
                "TLS checks off, unignored secret files, unpinned dependencies). This is a checklist, not an audit.")
    counts = {s: sum(1 for f in findings if f.severity == s) for s in ("high", "medium", "low")}
    head = f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low."
    return head + " " + " ".join(f.line_text() for f in findings[:6]) + (
        f" (and {len(findings) - 6} more)" if len(findings) > 6 else "")
