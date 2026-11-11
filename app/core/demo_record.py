"""Teach by demonstration with your own clicks - the narrow, safe version. You start a session, click through a routine, stop, and review the draft.

What is recorded, only while a session you started is running (at most ``MAX_SECONDS``): for each LEFT CLICK, the program in front and the NAME and type of
the control under the pointer (from Windows UI Automation). That is all.
Never recorded: any key you press (there is no keyboard hook at all - typed text, passwords and shortcuts are invisible to this), pointer movement,
screen pictures, right clicks, drags. Clicks are dropped when: the control is a password field; a privacy zone is active (password manager, banking,
private browsing, a meeting, your own zones); the click is on Jarvis itself. A control with no name is counted but cannot be replayed (a name is the only
thing a replay can press).
The result is a DRAFT: "open <program>", then "press <control name>" steps. Nothing is saved until you approve it; a saved workflow replays through the
normal tools, so every press still asks as usual. Typing steps must be added by telling Jarvis - they are not captured on purpose.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_SECONDS = 300.0
MAX_STEPS = 40


@dataclass(frozen=True)
class Click:
    process: str
    title: str
    name: str
    kind: str = "control"
    password: bool = False
    ours: bool = False


@dataclass
class Session:
    name: str
    started: float
    clicks: list[Click] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    def _drop(self, why: str) -> None:
        self.dropped[why] = self.dropped.get(why, 0) + 1

    def add(self, c: Click, zone_blocked: Callable[[str, str], str | None] = lambda p, t: None) -> str:
        """'kept' or the reason it was dropped."""
        if c.ours:
            self._drop("on Jarvis itself")
            return "on Jarvis itself"
        if c.password:
            self._drop("password field")
            return "password field"
        why = zone_blocked(c.process, c.title)
        if why:
            self._drop("privacy zone")
            return "privacy zone"
        if not c.name.strip():
            self._drop("control has no name")
            return "control has no name"
        if len(self.clicks) >= MAX_STEPS:
            self._drop("over the step limit")
            return "over the step limit"
        if self.clicks and self.clicks[-1] == c:
            self._drop("repeated click")
            return "repeated click"
        self.clicks.append(c)
        return "kept"


def to_steps(session: Session) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    current = None
    for c in session.clicks:
        proc = c.process.lower().removesuffix(".exe")
        if proc and proc != current:
            steps.append({"tool": "open_application", "arguments": {"name": proc}})
            current = proc
        steps.append({"tool": "press_control", "arguments": {"name": c.name.strip()[:80]}})
    return steps[:MAX_STEPS]


def describe(session: Session, steps: list[dict[str, Any]]) -> str:
    if not steps:
        text = "No replayable clicks were recorded."
    else:
        text = f"Draft '{session.name}' ({len(steps)} step(s)):\n" + "\n".join(
            f"  {i}. " + ("open " + s["arguments"]["name"] if s["tool"] == "open_application" else f"press '{s['arguments']['name']}'") for i, s in enumerate(steps, 1))
    if session.dropped:
        text += "\nLeft out: " + "; ".join(f"{n} x {why}" for why, n in session.dropped.items()) + "."
    return text + "\nKeys were not recorded (typing steps must be added by telling me). Nothing is saved until you say 'save'."


class ClickPoller:
    """Polls the left button and reads the control under the pointer on each press. No hooks. ``read`` and ``pressed`` are injected for tests."""

    def __init__(self, session: Session, pressed: Callable[[], bool], read: Callable[[], Click | None], zone_blocked: Callable[[str, str], str | None] = lambda p, t: None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, max_seconds: float = MAX_SECONDS) -> None:
        self.session, self.pressed, self.read, self.zone_blocked = session, pressed, read, zone_blocked
        self.clock, self.sleep, self.max_seconds = clock, sleep, max_seconds
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.ended = ""

    def run(self) -> None:
        start, down = self.clock(), False
        while not self._stop.is_set():
            if self.clock() - start >= self.max_seconds:
                self.ended = "time limit"
                break
            now_down = bool(self.pressed())
            if now_down and not down:                               # a press edge
                try:
                    click = self.read()
                except Exception:
                    click = None
                if click is not None:
                    self.session.add(click, self.zone_blocked)
            down = now_down
            self.sleep(0.03)
        self.ended = self.ended or "stopped"

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="demo-record", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


ACTIVE: ClickPoller | None = None
LAST: Session | None = None


def windows_readers() -> tuple[Callable[[], bool], Callable[[], Click | None]]:
    """Real readers (Windows only): left-button state and the UI Automation element under the pointer."""
    import ctypes
    from ctypes import wintypes

    from app.tools import winapi
    from app.tools.browser_tabs import _automation
    from app.tools.ui_elements import PRESSABLE

    found = _automation()
    if found is None:
        raise RuntimeError("UI Automation is not available on this PC")
    uia, _ = found
    user32 = ctypes.windll.user32

    def pressed() -> bool:
        return bool(user32.GetAsyncKeyState(0x01) & 0x8000)

    def read() -> Click | None:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        el = uia.ElementFromPoint(pt)
        hwnd = winapi.foreground_window()
        process = ""
        try:
            import psutil

            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = psutil.Process(pid.value).name()
        except Exception:
            pass
        return Click(process, winapi.window_title(hwnd), str(el.CurrentName or ""), PRESSABLE.get(el.CurrentControlType, "control"), bool(el.CurrentIsPassword), winapi.foreground_is_ours())

    return pressed, read
