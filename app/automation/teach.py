"""Teach by showing - the safe version.

"Teach Jarvis a routine": you start a teach session, then do the routine by telling Jarvis what to do ("open Chrome",
"open my project folder", "set the volume to 30"). Every tool Jarvis actually ran successfully during the session is
remembered; when you say you're done, the steps are shown to you and, if you agree, saved as a workflow.

What this deliberately is NOT: it never records your keyboard or mouse, your screen, or what you do outside Jarvis (a
global input recorder is a keylogger, however it is meant). It only sees the tool calls Jarvis itself made, with the
arguments it used. Passwords or keys in arguments are never kept; tools automations may not use are skipped.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from app.automation.tasks import FORBIDDEN_STEP_TOOLS, MAX_STEPS
from app.security.permissions import RiskLevel
from app.utils.redaction import redact

NEVER_RECORDED = FORBIDDEN_STEP_TOOLS | {"teach_workflow", "phone_control", "send_email", "run_command"}


@dataclass
class TeachSession:
    name: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class Teacher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.session: TeachSession | None = None

    @property
    def active(self) -> bool:
        return self.session is not None

    def start(self, name: str) -> TeachSession:
        with self._lock:
            self.session = TeachSession(name)
            return self.session

    def cancel(self) -> TeachSession | None:
        with self._lock:
            session, self.session = self.session, None
            return session

    def record(self, tool: str, arguments: dict[str, Any], level: RiskLevel) -> None:
        """Called after a tool really worked. Never raises."""
        with self._lock:
            session = self.session
            if session is None:
                return
            if tool in NEVER_RECORDED:
                session.skipped.append(f"{tool} (automations can't use it)")
                return
            if level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL):
                session.skipped.append(f"{tool} (too risky to repeat on its own)")
                return
            try:
                text = json.dumps(arguments, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                session.skipped.append(f"{tool} (arguments can't be saved)")
                return
            if redact(text) != text:
                session.skipped.append(f"{tool} (its arguments contain something secret)")
                return
            if len(session.steps) >= MAX_STEPS:
                session.skipped.append(f"{tool} (a workflow holds at most {MAX_STEPS} steps)")
                return
            session.steps.append({"tool": tool, "arguments": dict(arguments)})


TEACHER = Teacher()
