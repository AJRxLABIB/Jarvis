"""Attention firewall + mode-aware scheduling: WHEN may Jarvis interrupt you, and WHICH background work may run now.

One place, deterministic (never decided by the AI):

``interrupt``  - for a notice: "now" (shown and, if the mode allows, spoken), "digest" (waits in the notification center) or "hold" (waits until the
                 interruption ends). Inputs: priority, category, the current mode, focus timer, whether a full-screen app is in front, a meeting.
                 Urgent security notices and emergencies are never delayed. Everything held is still stored - nothing is dropped.
``allow``      - for a job: "critical" (never held), "light" (metrics, safety scans), "background" (backup, re-checks), "heavy" (benchmarks, big
                 indexing, fine-tuning), "network" (needs the internet). Uses the mode and the measured performance plan (``core/perf.py``).
                 A routine backup is the one chore the callers still force after 3 days.

Unknown input never silences anything important: if the full-screen state cannot be read it is "unknown" and treated as "not full screen" (so
an urgent notice is not lost), and only non-urgent notices are ever delayed. The Windows full-screen reader is unverified on real hardware.
"""

from __future__ import annotations

from typing import Callable

from app.core import modes as M

KINDS = ("critical", "light", "background", "heavy", "network")
_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
ALWAYS_NOW = {"security", "emergency"}


def interrupt(priority: str, category: str = "", *, mode: M.Mode | None = None, focus_active: bool = False,
              fullscreen: bool | None = None, meeting: bool = False) -> tuple[str, str]:
    """-> (when, reason). when: now | digest | hold."""
    mode = mode or M.STATE.current
    rank = _RANK.get(priority, 1)
    if rank >= 3 and (category in ALWAYS_NOW or mode.name != "presentation"):
        return "now", "urgent notices are never delayed" if category in ALWAYS_NOW else "urgent"
    if focus_active and rank < 3:
        return "hold", "a focus block is running"
    if fullscreen and rank < 3:
        return "hold", "a full-screen app is in front"
    if meeting and rank < 3:
        return "hold", "you are in a meeting"
    if priority not in mode.speak:
        return "digest", f"{mode.name} mode keeps {priority} notices in the center"
    return "now", ""


def allow(kind: str, *, mode: M.Mode | None = None, plan=None) -> tuple[bool, str]:
    """Whether a job of this kind may start now, and why not."""
    mode = mode or M.STATE.current
    if kind not in KINDS:
        return True, ""
    if kind in ("critical", "light"):
        return True, ""
    if kind == "network" and not mode.network:
        return False, f"{mode.name} mode: nothing that needs the internet runs"
    if kind == "heavy" and not mode.heavy:
        return False, f"{mode.name} mode: heavy work waits"
    if kind == "background" and not mode.background:
        return False, f"{mode.name} mode: chores wait"
    if plan is not None and kind in ("background", "heavy") and getattr(plan, "defer_background", False):
        return False, "the PC is busy or on low battery: chores wait"
    return True, ""


def fullscreen_reader() -> Callable[[], bool | None]:
    """A reader for 'is the foreground window covering a whole monitor?'. None = cannot tell (not Windows / call failed). Unverified on hardware."""
    def read() -> bool | None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            mon = user32.MonitorFromWindow(hwnd, 2)             # MONITOR_DEFAULTTONEAREST

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
                return None
            m = info.rcMonitor
            covers = (rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom)
            if not covers:
                return False
            buf = ctypes.create_unicode_buffer(64)           # the desktop / shell cover the screen too; they are not "an app"
            user32.GetClassNameW(hwnd, buf, 64)
            return buf.value not in ("Progman", "WorkerW", "Shell_TrayWnd")
        except Exception:
            return None

    return read


def may_speak(priority: str, category: str = "", *, focus_active: bool = False, reader: Callable[[], bool | None] | None = None,
              meeting_reader: Callable[[], bool] | None = None) -> bool:
    """For the spoken path (``speak_up``): True when the notice may be spoken now. It is shown either way."""
    state = (reader or fullscreen_reader())()
    meeting = False
    if _RANK.get(priority, 1) < 3:                    # only non-urgent notices need the (slower) meeting check
        try:
            from app.core import meeting as M

            meeting = meeting_reader() if meeting_reader is not None else M.in_meeting().in_meeting
        except Exception:
            meeting = False
    when, _ = interrupt(priority, category, focus_active=focus_active, fullscreen=bool(state), meeting=meeting)
    return when == "now"
