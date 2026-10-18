"""The Context Engine: what you are doing right now, as one structured snapshot.

It pulls together what Jarvis is already allowed to see - the window you're working in (not the screen), which
known project and file that window belongs to, that project's Git branch and uncommitted changes, your active
goal and mission, the approval waiting, recent actions Jarvis did, unread notifications, the PC's state, the
mode and the emergency switches - and says for each item where it came from and how sure it is:

* known    - read directly (e.g. the foreground window's title, git's own answer)
* likely   - inferred (e.g. the project, from the editor's title matching a project you told Jarvis about)
* unknown  - not available now (and why)

Nothing here is stored permanently: the last snapshots are kept in memory for a few minutes (retention below), so
"run the tests" can mean the project you're editing. In Private mode, and with CONTEXT_WINDOW_TITLES=false,
window titles aren't read. The screen is never captured by the context engine.
"""

from __future__ import annotations

from app.utils.timefmt import time12

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.utils.logging_setup import get_logger
from app.utils.redaction import redact

_log = get_logger("core.context")

KEEP_SNAPSHOTS = 30
RETENTION_SECONDS = 15 * 60  # snapshots older than this are dropped, never written to disk
FRESH_SECONDS = 120  # "the project you're working on" counts for this long after it was seen
GIT_CACHE_SECONDS = 30
KNOWN, LIKELY, UNKNOWN = "known", "likely", "unknown"

EDITORS = {"code.exe": "VS Code", "code - insiders.exe": "VS Code Insiders", "cursor.exe": "Cursor",
           "pycharm64.exe": "PyCharm", "idea64.exe": "IntelliJ IDEA", "webstorm64.exe": "WebStorm",
           "phpstorm64.exe": "PhpStorm", "sublime_text.exe": "Sublime Text", "notepad++.exe": "Notepad++",
           "devenv.exe": "Visual Studio", "zed.exe": "Zed", "windsurf.exe": "Windsurf"}
BROWSERS = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "brave.exe": "Brave",
            "opera.exe": "Opera", "vivaldi.exe": "Vivaldi"}
TERMINALS = {"windowsterminal.exe": "Windows Terminal", "powershell.exe": "PowerShell", "pwsh.exe": "PowerShell",
             "cmd.exe": "Command Prompt", "wezterm-gui.exe": "WezTerm"}
_SPLIT = re.compile(r"\s+[-–—|]\s+")
_FILE = re.compile(r"[\w.-]+\.[A-Za-z0-9]{1,8}")


@dataclass(frozen=True)
class Fact:
    value: Any
    source: str
    certainty: str = KNOWN  # known | likely | unknown

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "source": self.source, "certainty": self.certainty}


def unknown(why: str) -> Fact:
    return Fact(None, why, UNKNOWN)


@dataclass
class ContextSnapshot:
    timestamp: float
    facts: dict[str, Fact] = field(default_factory=dict)

    def value(self, name: str, default: Any = None) -> Any:
        fact = self.facts.get(name)
        return fact.value if fact is not None and fact.certainty != UNKNOWN else default

    @property
    def confidence(self) -> float:
        """Share of the core facts that are known (1.0) or likely (0.6) - a measure of coverage, not of truth."""
        core = ("active_app", "active_window", "project", "git_branch", "current_goal", "device_state")
        score = sum(1.0 if self.facts.get(n, unknown("")).certainty == KNOWN else
                    0.6 if self.facts.get(n, unknown("")).certainty == LIKELY else 0.0 for n in core)
        return round(score / len(core), 2)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"timestamp": datetime.fromtimestamp(self.timestamp).astimezone().isoformat(
            timespec="seconds"), "confidence": self.confidence}
        out.update({name: fact.value for name, fact in self.facts.items() if fact.certainty != UNKNOWN})
        out["sources"] = {name: fact.as_dict() for name, fact in self.facts.items()}
        return out

    def line(self) -> str:
        """One short line for the model's prompt: only what is known or likely."""
        parts = []
        app, project, file = self.value("active_app"), self.value("project"), self.value("current_file")
        if app:
            parts.append(f"user is in {app}")
        if project:
            git = []
            if self.value("git_branch"):
                git.append(f"branch {self.value('git_branch')}")
            if self.value("git_changes") is not None:
                git.append(f"{self.value('git_changes')} uncommitted change(s)")
            parts.append(f"project {project}" + (f" ({', '.join(git)})" if git else ""))
        if file:
            parts.append(f"file {file}")
        if self.value("browser_page"):
            parts.append(f"web page '{self.value('browser_page')}'")
        if self.value("current_goal"):
            parts.append(f"goal: {self.value('current_goal')}")
        if self.value("current_mission"):
            parts.append(f"mission: {self.value('current_mission')}")
        if self.value("pending_approval"):
            parts.append(f"waiting for approval: {self.value('pending_approval')}")
        return ("Right now: " + "; ".join(parts) + ".") if parts else ""


# ------------------------------------------------------------------ reading the window


def parse_window(title: str, process: str, projects: list[Path]) -> dict[str, Fact]:
    """What a window's title says: the app, the file, the project (matched to projects you told Jarvis about)."""
    facts: dict[str, Fact] = {}
    process = (process or "").lower()
    app = EDITORS.get(process) or BROWSERS.get(process) or TERMINALS.get(process)
    parts = [p.strip() for p in _SPLIT.split(title or "") if p.strip()]
    if app is None:
        app = parts[-1] if len(parts) > 1 else (process.removesuffix(".exe") or None)
    if app:
        facts["active_app"] = Fact(app, "the foreground window", KNOWN)
    if title:
        facts["active_window"] = Fact(redact(title)[:160], "the foreground window's title", KNOWN)
    if process in EDITORS:
        facts["editor"] = Fact(EDITORS[process], "the foreground window", KNOWN)
        names = {p.name.lower(): p for p in projects}
        found = next((names[part.lower()] for part in parts if part.lower() in names), None)
        if found is None:  # a path in the title (Notepad++, Visual Studio)
            found = next((p for p in projects if str(p).lower() in (title or "").lower()), None)
        if found is not None:
            facts["project"] = Fact(found.name, f"{EDITORS[process]}'s title matches a project you told me about",
                                    LIKELY)
            facts["workspace"] = Fact(str(found), "that project's folder", LIKELY)
        file = next((part for part in parts if _FILE.fullmatch(part.lstrip("● ").strip())), None)
        if file:
            facts["current_file"] = Fact(file.lstrip("● ").strip(), f"{EDITORS[process]}'s title", LIKELY)
            if part_unsaved(parts):
                facts["unsaved_changes"] = Fact(True, f"{EDITORS[process]} marks the file as changed", LIKELY)
    elif process in BROWSERS and len(parts) > 1:
        facts["browser_page"] = Fact(redact(" - ".join(parts[:-1]))[:120], "the browser window's title", KNOWN)
    elif process in TERMINALS:
        facts["terminal"] = Fact(parts[0][:80] if parts else app, "the terminal window's title", KNOWN)
    return facts


def part_unsaved(parts: list[str]) -> bool:
    return bool(parts) and parts[0].startswith("●")


# ------------------------------------------------------------------ the engine


class ContextEngine:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._history: deque[ContextSnapshot] = deque(maxlen=KEEP_SNAPSHOTS)
        self._git: dict[str, tuple[float, dict[str, Any]]] = {}
        self.pending_approval = None  # callback () -> str | None (the window's question waiting)
        self.last_request = ""  # what you asked last (set by the conversation)

    # the window: patched in tests
    @staticmethod
    def _window():
        from app.tools import winapi

        if not winapi.is_windows():
            return None
        return winapi.target_window()

    def _projects(self, ctx) -> list[Path]:
        try:
            from app.core.projects import known_project_folders

            return known_project_folders(ctx)
        except Exception:
            return []

    def _git_info(self, folder: str, ctx) -> dict[str, Any]:
        now = self._clock()
        cached = self._git.get(folder)
        if cached is not None and now - cached[0] < GIT_CACHE_SECONDS:
            return cached[1]
        from app.core.projects import _git

        try:
            info = _git(Path(folder), ctx)
        except Exception:
            info = {}
        self._git[folder] = (now, info)
        return info

    def snapshot(self, ctx) -> ContextSnapshot:
        """Read the context now (cheap: no screen capture; git is cached for 30 s)."""
        from app.core import emergency, modes

        snap = ContextSnapshot(self._clock())
        facts = snap.facts
        mode = modes.STATE.current
        facts["jarvis_mode"] = Fact(mode.name, "the mode you chose", KNOWN)
        switches = emergency.STATE.active()
        if switches:
            facts["emergency"] = Fact(switches, "the emergency controls", KNOWN)
        titles_allowed = getattr(ctx.settings, "context_window_titles", True) and mode.capture
        projects = self._projects(ctx)
        if not titles_allowed:
            facts["active_window"] = unknown("window titles are off (Private mode or CONTEXT_WINDOW_TITLES=false)")
        else:
            try:
                window = self._window()
            except Exception:
                window = None
            if window is None:
                facts["active_window"] = unknown("no window I can read (not on Windows, or nothing open)")
            else:
                facts.update(parse_window(window.title, window.process_name, projects))
        if "project" not in facts:
            recent = self.recent_project()
            if recent is not None:
                facts["project"] = Fact(recent[0], "the project you were in a moment ago", LIKELY)
                facts["workspace"] = Fact(recent[1], "that project's folder", LIKELY)
            elif len(projects) == 1:
                facts["project"] = Fact(projects[0].name, "your only known project", LIKELY)
                facts["workspace"] = Fact(str(projects[0]), "that project's folder", LIKELY)
            else:
                facts["project"] = unknown("no known project is open in an editor")
        workspace = snap.value("workspace")
        if workspace:
            git = self._git_info(workspace, ctx)
            if git.get("branch"):
                facts["git_branch"] = Fact(git["branch"], "git status", KNOWN)
                facts["git_changes"] = Fact(int(git.get("changes", 0)), "git status", KNOWN)
            else:
                facts["git_branch"] = unknown("not a git repository, or git isn't installed")
        self._goals(ctx, facts)
        self._recent(ctx, facts)
        self._device(ctx, facts)
        if self.pending_approval is not None:
            try:
                question = self.pending_approval()
            except Exception:
                question = None
            if question:
                facts["pending_approval"] = Fact(str(question)[:160], "the approval queue", KNOWN)
        if self.last_request:
            facts["last_request"] = Fact(redact(self.last_request)[:160], "what you asked last", KNOWN)
        self._remember(snap)
        return snap

    def _goals(self, ctx, facts: dict[str, Fact]) -> None:
        if ctx.database is None:
            return
        try:
            from app.core.goals import GoalStore
            from app.core.missions import MissionStore

            goals = GoalStore(ctx.database).active()
            if goals:
                goal = goals[0]
                facts["current_goal"] = Fact(f"{goal.title} ({goal.progress}%" + (f", due {goal.deadline}" if
                                             goal.deadline else "") + ")", "your goals", KNOWN)
            mission = MissionStore(ctx.database).unfinished()
            if mission is not None:
                facts["current_mission"] = Fact(f"{mission.title} ({mission.state}, {mission.progress()})",
                                                "Mission Control", KNOWN)
        except Exception:
            _log.warning("context: goals/missions unreadable", exc_info=True)

    def _recent(self, ctx, facts: dict[str, Fact]) -> None:
        if ctx.database is None:
            return
        try:
            from datetime import timedelta

            from app.agent.activity import current_session

            actions = current_session(ctx.database, datetime.now().astimezone(), lookback=timedelta(hours=3))[-5:]
            if actions:
                facts["recent_actions"] = Fact([f"{time12(a.at)} {a.tool}" for a in actions],
                                               "what Jarvis did for you (audit log)", KNOWN)
            with ctx.database.connect() as conn:
                unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE read = 0 AND archived = 0").fetchone()[0]
            facts["unread_notifications"] = Fact(int(unread), "the notification center", KNOWN)
        except Exception:
            _log.warning("context: recent actions unreadable", exc_info=True)

    def _device(self, ctx, facts: dict[str, Fact]) -> None:
        state: dict[str, Any] = {}
        try:
            import psutil

            battery = psutil.sensors_battery()
            if battery is not None:
                state["battery"] = round(battery.percent)
                state["charging"] = bool(battery.power_plugged)
            state["cpu"] = round(psutil.cpu_percent(interval=None))
            state["ram"] = round(psutil.virtual_memory().percent)
        except Exception:
            pass
        try:
            from app.utils import network

            state["online"] = not network.is_offline_now(ctx.settings)
        except Exception:
            pass
        facts["device_state"] = Fact(state, "this PC (psutil, network check)", KNOWN) if state else \
            unknown("the PC's state couldn't be read")

    # ------------------------------------------------------------ short-term history (memory only)

    def _remember(self, snap: ContextSnapshot) -> None:
        with self._lock:
            self._history.append(snap)
            while self._history and snap.timestamp - self._history[0].timestamp > RETENTION_SECONDS:
                self._history.popleft()

    def history(self) -> list[ContextSnapshot]:
        now = self._clock()
        with self._lock:
            return [s for s in self._history if now - s.timestamp <= RETENTION_SECONDS]

    def recent_project(self) -> tuple[str, str] | None:
        """(name, folder) of the project seen in an editor within FRESH_SECONDS."""
        now = self._clock()
        with self._lock:
            for snap in reversed(self._history):
                if now - snap.timestamp > FRESH_SECONDS:
                    break
                fact = snap.facts.get("workspace")
                if fact is not None and fact.certainty != UNKNOWN and "a moment ago" not in fact.source \
                        and snap.facts["project"].source != "your only known project":
                    return snap.value("project"), fact.value
        return None

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
            self._git.clear()


ENGINE = ContextEngine()


def describe(snap: ContextSnapshot) -> str:
    """For 'what do you see / what am I working on': known and likely facts, with how Jarvis knows them."""
    labels = {"active_app": "App", "active_window": "Window", "project": "Project", "current_file": "File",
              "git_branch": "Git branch", "git_changes": "Uncommitted changes", "browser_page": "Web page",
              "terminal": "Terminal", "current_goal": "Goal", "current_mission": "Mission",
              "pending_approval": "Waiting for approval", "unread_notifications": "Unread notifications",
              "jarvis_mode": "Mode", "emergency": "Emergency switches"}
    lines = []
    for name, label in labels.items():
        fact = snap.facts.get(name)
        if fact is None or fact.certainty == UNKNOWN:
            continue
        value = ", ".join(fact.value) if isinstance(fact.value, list) else fact.value
        hedge = " (probably)" if fact.certainty == LIKELY else ""
        lines.append(f"{label}: {value}{hedge}")
    state = snap.value("device_state") or {}
    if state:
        bits = [f"CPU {state['cpu']}%" if "cpu" in state else "", f"RAM {state['ram']}%" if "ram" in state else "",
                f"battery {state['battery']}%" if "battery" in state else "",
                ("online" if state.get("online") else "offline") if "online" in state else ""]
        lines.append("PC: " + ", ".join(b for b in bits if b))
    unknown_bits = [f"{labels.get(n, n)} ({f.source})" for n, f in snap.facts.items()
                    if f.certainty == UNKNOWN and n in labels]
    if unknown_bits:
        lines.append("Not known right now: " + "; ".join(unknown_bits))
    return "\n".join(lines) if lines else "I can't see anything about what you're doing right now."
