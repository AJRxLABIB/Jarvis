"""Meeting detector: are you probably in a call right now? Used by the attention firewall (core/attention.py) to hold non-urgent notices.

Evidence, in order of strength (never a single weak signal):
* STRONG  - the microphone is in use by an app other than Jarvis AND the app in front is a known meeting app (Zoom, Teams, Webex, Skype, Slack huddle,
            Discord, Meet / Teams / Zoom in a browser tab title);
* PROBABLE- a known meeting app is in front with a meeting-like window title ("Meeting", "Call", "| Microsoft Teams") but the microphone state is
            unknown;
* NO      - everything else. The microphone alone is NOT a meeting (dictation, a game, a recorder), and Jarvis's own listening is ignored.
The answer carries its reason and a confidence, and "unknown" is never turned into "in a meeting". Pure ``detect`` over injected facts; the Windows
readers (foreground program, microphone consent store) are best-effort and unverified on real hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

MEETING_APPS = {"zoom", "teams", "ms-teams", "msteams", "webex", "webexmta", "skype", "lync", "slack", "discord", "bluejeans", "gotomeeting", "whereby"}
BROWSERS = {"chrome", "msedge", "firefox", "brave", "opera", "vivaldi"}
_TITLE = ("meet.google.com", "google meet", "meet -", "zoom meeting", "microsoft teams", "| teams", "teams meeting", "webex", "huddle", "whereby", " call")


@dataclass(frozen=True)
class Facts:
    process: str = ""                 # lower-case program name without .exe
    title: str = ""
    mic_in_use: bool | None = None    # None = could not read
    jarvis_listening: bool = False


@dataclass(frozen=True)
class Verdict:
    in_meeting: bool
    confidence: str                   # high | medium | low
    reason: str


def detect(f: Facts) -> Verdict:
    proc = f.process.lower().removesuffix(".exe")
    title = f.title.lower()
    app = proc in MEETING_APPS
    web = proc in BROWSERS and any(t in title for t in _TITLE)
    mic = f.mic_in_use and not f.jarvis_listening
    if (app or web) and mic:
        return Verdict(True, "high", f"{'a browser meeting' if web else proc} is in front and the microphone is in use")
    if (app or web) and f.mic_in_use is None and any(t in title for t in _TITLE + ("meeting",)):
        return Verdict(True, "low", f"{proc} looks like a meeting window, but I cannot read the microphone state")
    if mic:
        return Verdict(False, "medium", "the microphone is in use, but not by a meeting app in front (not treated as a meeting)")
    return Verdict(False, "medium" if f.mic_in_use is not None else "low", "no meeting app in front with the microphone in use")


def real_facts(listening: Callable[[], bool] = lambda: False) -> Facts:
    """Foreground program + microphone consent store (Windows). Anything unreadable stays unknown. Unverified on hardware."""
    process = title = ""
    mic: bool | None = None
    try:
        import ctypes

        import psutil

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = psutil.Process(pid.value).name().lower().removesuffix(".exe")
            buf = ctypes.create_unicode_buffer(200)
            user32.GetWindowTextW(hwnd, buf, 200)
            title = buf.value
    except Exception:
        pass
    try:
        import winreg

        base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
        in_use = False
        for sub in ("", r"\NonPackaged"):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + sub) as root:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(root, name) as key:
                            start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
                            stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
                            if start and not stop:          # started and not stopped = in use right now
                                in_use = True
                    except OSError:
                        continue
        mic = in_use
    except Exception:
        mic = None
    try:
        own = bool(listening())
    except Exception:
        own = False
    return Facts(process, title, mic, own)


def in_meeting(listening: Callable[[], bool] = lambda: False, reader: Callable[[], Facts] | None = None) -> Verdict:
    return detect((reader or (lambda: real_facts(listening)))())
