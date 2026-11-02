"""Verified project start: did the dev server really come up, or did it die, or is it just sitting there?

``run_project`` starts a project's usual command and stops there ("Started ..."). A started process is not a working server. This module reads what the
process printed (its log) and what the machine says (is it still running, does its port answer) and turns that into one honest state:

  starting           running, grace period not over, nothing listening yet
  running            running AND a port it announced answers on this PC            <- the only state that is "verified up"
  running_with_errors  up, but the log shows errors since start
  unverified         running, but no listening port was seen (a worker / watcher, or a slow build) - not claimed as up
  crashed            it ended with a non-zero exit code (the cause is read from the log)
  finished           it ended with exit code 0 (not a server)

Errors in the log are matched against a fixed table (missing module, port in use, syntax error, ...) with a plain explanation and the NEXT STEP as text.
Nothing here edits files, installs anything or runs commands: the log is untrusted data and only ever selects one of the fixed explanations.
Pure logic over injected facts (exit code, log text, a port probe, a clock), so it is fully tested; the process side is in ``tools/dev_session_tool.py``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

STARTING, RUNNING, RUNNING_ERRORS, UNVERIFIED, CRASHED, FINISHED = "starting", "running", "running_with_errors", "unverified", "crashed", "finished"
SETTLED = {RUNNING, RUNNING_ERRORS, UNVERIFIED, CRASHED, FINISHED}
GRACE_SECONDS = 20.0
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|\[::\]):(\d{2,5})", re.I)
_LISTEN = re.compile(r"(?:listening|running|serving|started|server)\b[^\n]{0,40}?\bport\s*[:=]?\s*(\d{2,5})|\bport\s*(\d{2,5})\s*(?:is\s+)?(?:open|ready|listening)", re.I)
_LOCAL_COLON = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})", re.I)


@dataclass(frozen=True)
class Finding:
    kind: str
    summary: str
    evidence: str
    advice: str
    where: str = ""

    def line(self) -> str:
        return f"{self.summary}" + (f" ({self.where})" if self.where else "") + f" - next: {self.advice}"


# (regex, kind, summary template using named groups, advice)
_RULES: tuple[tuple[re.Pattern[str], str, str, str], ...] = tuple((re.compile(p, re.I), k, s, a) for p, k, s, a in (
    (r"ModuleNotFoundError: No module named '?(?P<m>[\w.\-]+)'?", "missing_module", "a Python package is missing: {m}",
     "install it (pip install {m}) in the project's environment - I can run that if you say so"),
    (r"Cannot find module '(?P<m>[^']+)'", "missing_module", "a Node package is missing: {m}", "run npm install in the project - I can do that if you say so"),
    (r"(?:EADDRINUSE|address already in use|Port (?P<p>\d+) is (?:already )?in use|Errno 98|WinError 10048)", "port_in_use", "the port is already in use{p}",
     "something else is using it: stop that program, or start this one on another port"),
    (r"(?P<k>SyntaxError|IndentationError|TabError): (?P<m>[^\n]{0,80})", "syntax_error", "{k}: {m}", "fix that line; I can read the file and propose an edit"),
    (r"(?:is not recognized as an internal or external command|command not found|ENOENT[^\n]{0,40}spawn (?P<m>\S+))", "tool_missing",
     "a program the project needs is not installed or not on PATH", "install it or open a new terminal so PATH refreshes"),
    (r"(?:PermissionError|EACCES|Access is denied)", "permission", "a permission error (file or port)", "check the folder permissions; do not run the project as administrator just to hide this"),
    (r"(?:ECONNREFUSED|Connection refused|could not connect to server|Can't connect to MySQL|OperationalError)", "dependency_down",
     "the project could not reach a service it needs (a database?)", "start that service first (for example MySQL / Postgres / XAMPP), then start the project again"),
    (r"(?:npm ERR!|npm error)\s*(?P<m>[^\n]{0,80})", "npm_error", "npm reported an error: {m}", "read the npm lines in the log; often a script name or dependency problem"),
    (r"(?P<m>(?:Traceback \(most recent call last\))|(?:Unhandled(?:Promise)?Rejection)|(?:Uncaught \w*Error))", "exception", "the project raised an unhandled error",
     "read the last lines of the log; I can open the file at the line it names"),
))
_FILE_LINE = re.compile(r'(?:File "(?P<py>[^"]+)", line (?P<pl>\d+)|(?P<js>[\w./\\:-]+\.(?:js|ts|tsx|jsx|php|py|java|go|rs)):(?P<jl>\d+))')


def clean(text: str) -> str:
    return _ANSI.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n")


def ports_announced(log: str) -> list[int]:
    """Ports the project says it listens on, in the order printed (deduplicated, sane range)."""
    text = clean(log)
    found: list[int] = []
    for rx in (_URL, _LOCAL_COLON, _LISTEN):
        for m in rx.finditer(text):
            raw = next((g for g in m.groups() if g), None)
            if raw and 1 <= int(raw) <= 65535 and int(raw) not in found:
                found.append(int(raw))
    return found


def find_errors(log: str, limit: int = 4) -> list[Finding]:
    """Known problems in the log, most specific first. Each pattern reports at most once."""
    text = clean(log)
    out: list[Finding] = []
    for rx, kind, summary, advice in _RULES:
        m = rx.search(text)
        if not m:
            continue
        groups = {k: (v or "") for k, v in m.groupdict().items()}
        if "p" in groups and groups["p"]:
            groups["p"] = f" ({groups['p']})"
        line_start = text.rfind("\n", 0, m.start()) + 1
        evidence = text[line_start:text.find("\n", m.end()) if text.find("\n", m.end()) != -1 else len(text)].strip()[:200]
        where = ""
        near = list(_FILE_LINE.finditer(text[max(0, m.start() - 1500):m.end() + 300 if kind != "exception" else m.start() + 4000]))
        fl = near[-1] if near else None                       # the last file:line before the error is the frame that failed
        if fl:
            where = f"{(fl.group('py') or fl.group('js'))[-60:]}:{fl.group('pl') or fl.group('jl')}"
        out.append(Finding(kind, summary.format(**groups), evidence, advice.format(**groups), where))
        if len(out) >= limit:
            break
    return out


@dataclass
class Report:
    state: str
    elapsed: float
    port: int | None = None
    url: str = ""
    exit_code: int | None = None
    findings: list[Finding] = field(default_factory=list)
    tried_ports: list[int] = field(default_factory=list)

    @property
    def up(self) -> bool:
        return self.state in (RUNNING, RUNNING_ERRORS)

    def render(self, name: str = "the project") -> str:
        if self.state == RUNNING:
            head = f"{name} is running and answering on port {self.port}" + (f" ({self.url})" if self.url else "") + " - verified: the port accepted a connection."
        elif self.state == RUNNING_ERRORS:
            head = f"{name} is answering on port {self.port}, but its log shows errors:"
        elif self.state == STARTING:
            head = f"{name} is still starting ({self.elapsed:.0f}s so far); nothing is listening yet."
        elif self.state == UNVERIFIED:
            seen = f"It printed port(s) {', '.join(map(str, self.tried_ports))} but none answered." if self.tried_ports else "It never printed a port."
            head = f"{name} is running, but I could not verify a server: {seen} It may be a worker or a slow build - not reported as up."
        elif self.state == CRASHED:
            head = f"{name} stopped with exit code {self.exit_code}."
        else:
            head = f"{name} ran and finished (exit code 0); it is not a long-running server."
        lines = [head] + [f"- {f.line()}" for f in self.findings]
        return "\n".join(lines)


def assess(*, exit_code: int | None, log: str, elapsed: float, probe: Callable[[int], bool], grace: float = GRACE_SECONDS) -> Report:
    ports = ports_announced(log)
    findings = find_errors(log)
    if exit_code is not None:
        state = FINISHED if exit_code == 0 and not findings else CRASHED
        return Report(state, elapsed, exit_code=exit_code, findings=findings, tried_ports=ports)
    for port in ports:
        if probe(port):
            url = next((m.group(0) for m in _URL.finditer(clean(log)) if m.group(1) == str(port)), f"http://localhost:{port}")
            return Report(RUNNING_ERRORS if findings else RUNNING, elapsed, port=port, url=url.replace("0.0.0.0", "localhost"), findings=findings, tried_ports=ports)
    if findings and elapsed >= 2.0:                       # fatal-looking output but the process is still alive (a watcher that will retry)
        return Report(UNVERIFIED, elapsed, findings=findings, tried_ports=ports)
    return Report(STARTING if elapsed < grace else UNVERIFIED, elapsed, findings=findings, tried_ports=ports)


def wait_until_settled(poll: Callable[[], tuple[int | None, str]], probe: Callable[[int], bool], *, timeout: float = 20.0, interval: float = 0.5,
                       clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, grace: float | None = None) -> Report:
    """Poll ``poll() -> (exit_code, log_text)`` until the state settles or ``timeout`` passes (then the last, possibly "starting", report)."""
    start = clock()
    grace = timeout if grace is None else grace
    while True:
        code, log = poll()
        report = assess(exit_code=code, log=log, elapsed=clock() - start, probe=probe, grace=grace)
        if report.state in SETTLED or clock() - start >= timeout:
            return report
        sleep(interval)
