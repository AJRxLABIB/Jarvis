"""Runs an already-approved argument list. Never a shell, never stdin, always a timeout."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from app.tools.base import ToolError
from app.utils.logging_setup import get_logger, log_event
from app.utils.redaction import redact

_log = get_logger("commands")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
MAX_OUTPUT_CHARS = 3000
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


@dataclass(frozen=True)
class CommandOutput:
    argv: tuple[str, ...]
    returncode: int | None
    output: str
    timed_out: bool = False
    pid: int | None = None  # background processes


def resolve_program(name: str) -> str:
    """Absolute path of a program on PATH (npm -> ...\\npm.cmd). Raises if missing."""
    found = shutil.which(name)
    if not found:
        raise ToolError(f"'{name}' is not installed or not on PATH.", "not_installed")
    return found


def _tail(text: str) -> str:
    text = _ANSI.sub("", text).replace("\r\n", "\n").strip()
    if len(text) > MAX_OUTPUT_CHARS:
        text = "...(earlier output cut)...\n" + text[-MAX_OUTPUT_CHARS:]
    return redact(text)


class CommandExecutor:
    def __init__(self) -> None:
        self._background: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def run(self, argv: list[str] | tuple[str, ...], cwd: Path, timeout: float = 120.0) -> CommandOutput:
        program = resolve_program(argv[0])
        full = [program, *argv[1:]]
        log_event(_log, "command_started", argv=list(argv), cwd=str(cwd))
        try:
            proc = subprocess.Popen(
                full, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                shell=False, creationflags=_NO_WINDOW, env=_child_env(),
            )
        except OSError as exc:
            raise ToolError(f"Could not start {argv[0]}: {exc.strerror or exc}", "launch_failed") from exc
        try:
            out, _ = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, _ = proc.communicate()
            timed_out = True
        text = _tail(out.decode("utf-8", errors="replace") if out else "")
        log_event(_log, "command_finished", argv=list(argv), returncode=proc.returncode, timed_out=timed_out)
        return CommandOutput(tuple(argv), None if timed_out else proc.returncode, text, timed_out)

    def start_background(self, argv: list[str] | tuple[str, ...], cwd: Path) -> CommandOutput:
        """Long-running commands (dev servers) get their own console window."""
        program = resolve_program(argv[0])
        try:
            proc = subprocess.Popen(
                [program, *argv[1:]], cwd=str(cwd), stdin=subprocess.DEVNULL, shell=False,
                creationflags=_NEW_CONSOLE, env=_child_env(),
            )
        except OSError as exc:
            raise ToolError(f"Could not start {argv[0]}: {exc.strerror or exc}", "launch_failed") from exc
        with self._lock:
            self._background[proc.pid] = proc
        log_event(_log, "command_background", argv=list(argv), pid=proc.pid, cwd=str(cwd))
        return CommandOutput(tuple(argv), None, "", pid=proc.pid)

    def running(self) -> list[CommandOutput]:
        with self._lock:
            alive = {pid: p for pid, p in self._background.items() if p.poll() is None}
            self._background = alive
            return [CommandOutput(tuple(map(str, p.args if isinstance(p.args, list) else [p.args])), None, "", pid=pid)
                    for pid, p in alive.items()]


    def stop_all(self) -> int:
        """Emergency stop: end every background command Jarvis started (dev servers), with their child processes."""
        with self._lock:
            alive = [p for p in self._background.values() if p.poll() is None]
            self._background = {}
        for proc in alive:
            _kill_tree(proc)
        if alive:
            log_event(_log, "commands_stopped", count=len(alive))
        return len(alive)


def _child_env() -> dict[str, str]:
    """The child inherits the environment minus obvious secrets."""
    from app.utils.redaction import is_sensitive_key

    return {k: v for k, v in os.environ.items() if not is_sensitive_key(k)}


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        import psutil

        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except Exception:
        proc.kill()


EXECUTOR = CommandExecutor()
