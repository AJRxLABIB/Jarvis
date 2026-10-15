"""Policy for development commands ("run my tests", "git status").

The model never gets a shell. A command is split into an argument list and
checked here; ``CommandExecutor`` then runs that list with ``shell=False``.

    blocked  (CRITICAL)  system-altering programs, shells, script hosts
    unknown  (DANGEROUS) anything not in allowed_dev_commands -> always asks
    git-destructive (DANGEROUS) reset --hard, clean, push --force, branch -D ...
    allowlisted          the level configured in permissions.json
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath

from app.config.files import DevCommand, PermissionsConfig
from app.security.permissions import RiskLevel
from app.tools.base import ToolError

MAX_COMMAND_CHARS = 300
MAX_ARGS = 24
# Anything a shell would interpret. With shell=False they would be harmless, but
# Windows runs .cmd/.bat files (npm.cmd, yarn.cmd) through cmd.exe, which would
# interpret them, so they are refused everywhere.
_SHELL_CHARS = set("&|;<>^`$%!\r\n()")

BLOCKED_PROGRAMS = frozenset({
    "format", "diskpart", "cipher", "bcdedit", "reg", "regedit", "sc", "net", "net1", "shutdown", "del", "erase",
    "rd", "rmdir", "rm", "cmd", "powershell", "pwsh", "wmic", "vssadmin", "schtasks", "takeown", "icacls", "cacls",
    "attrib", "mshta", "rundll32", "regsvr32", "certutil", "bitsadmin", "wscript", "cscript", "msiexec", "runas",
    "netsh", "bash", "wsl", "sh", "fsutil", "mountvol", "manage-bde", "sfc", "dism", "setx",
})

_GIT_DANGEROUS: tuple[tuple[str, frozenset[str] | None], ...] = (
    ("reset", frozenset({"--hard", "--merge", "--keep"})),
    ("clean", None),  # any clean deletes untracked files
    ("push", frozenset({"-f", "--force", "--force-with-lease", "--force-if-includes", "--delete", "-d", "--mirror", "--prune"})),
    ("branch", frozenset({"-d", "-D", "--delete", "-f", "--force", "-m", "-M"})),
    ("checkout", frozenset({"--", ".", "-f", "--force"})),
    ("restore", None),
    ("rebase", None),
    ("filter-branch", None),
    ("filter-repo", None),
    ("stash", frozenset({"drop", "clear"})),
    ("tag", frozenset({"-d", "--delete", "-f", "--force"})),
    ("reflog", frozenset({"expire", "delete"})),
    ("gc", frozenset({"--prune=now", "--aggressive"})),
    ("rm", None),
    ("update-ref", None),
    ("worktree", frozenset({"remove", "prune"})),
)


def program_name(token: str) -> str:
    """'C:\\x\\npm.cmd' -> 'npm'."""
    name = PurePath(token.replace("\\", "/")).name.lower()  # works for Windows paths on any OS
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def split_command(command: str) -> list[str]:
    """Split a command line into arguments. Double quotes group words; nothing else is special."""
    text = command.strip()
    if not text:
        raise ToolError("Which command?", "missing_command")
    if len(text) > MAX_COMMAND_CHARS:
        raise ToolError("That command is too long.", "invalid_command")
    bad = sorted(set(text) & _SHELL_CHARS)
    if bad:
        raise ToolError(
            f"Commands can't contain shell characters ({' '.join(bad)}). Run one simple command at a time.",
            "invalid_command",
        )
    args = [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', text)]
    args = [a for a in args if a]
    if not args or len(args) > MAX_ARGS:
        raise ToolError("That command has too many parts.", "invalid_command")
    return args


@dataclass(frozen=True)
class CommandDecision:
    argv: tuple[str, ...]
    level: RiskLevel
    reason: str  # allowlisted | unknown | git_destructive | blocked
    rule: DevCommand | None = None

    @property
    def display(self) -> str:
        return " ".join(f'"{a}"' if " " in a else a for a in self.argv)


class CommandPolicy:
    def __init__(self, permissions: PermissionsConfig) -> None:
        self.allowed = permissions.allowed_dev_commands

    def _matching_rule(self, argv: list[str]) -> DevCommand | None:
        normalized = [program_name(argv[0]), *(a.lower() for a in argv[1:])]
        best: DevCommand | None = None
        for rule in self.allowed:
            prefix = [program_name(rule.argv[0]), *(a.lower() for a in rule.argv[1:])]
            if normalized[: len(prefix)] == prefix and (best is None or len(prefix) > len(best.argv)):
                best = rule
        return best

    @staticmethod
    def _git_destructive(argv: list[str]) -> bool:
        if program_name(argv[0]) != "git" or len(argv) < 2:
            return False
        sub, rest = argv[1].lower(), {a for a in argv[2:]}
        rest_lower = {a.lower() for a in rest}
        for name, flags in _GIT_DANGEROUS:
            if sub != name:
                continue
            if flags is None:
                return True
            if rest & flags or rest_lower & flags or any(a.startswith("--force") for a in rest_lower):
                return True
            if name == "push" and any(a.startswith((":", "+")) for a in rest):  # delete / force refspecs
                return True
        return False

    def evaluate(self, command: str | list[str]) -> CommandDecision:
        argv = split_command(command) if isinstance(command, str) else list(command)
        program = program_name(argv[0])
        if program in BLOCKED_PROGRAMS:
            return CommandDecision(tuple(argv), RiskLevel.CRITICAL, "blocked")
        if self._git_destructive(argv):
            return CommandDecision(tuple(argv), RiskLevel.DANGEROUS, "git_destructive")
        rule = self._matching_rule(argv)
        if rule is None:
            return CommandDecision(tuple(argv), RiskLevel.DANGEROUS, "unknown")
        return CommandDecision(tuple(argv), RiskLevel(rule.level), "allowlisted", rule)
