"""The PC's real state, as JARVIS Mobile shows it - measured, never assumed.

Two separate things, never merged:

* JARVIS authentication - the phone proved its key (app/remote/mobile.py). That says nothing about Windows.
* Windows session       - is the signed-in Windows session locked or active? Read from Windows
                          (WTSQuerySessionInformation WTSSessionInfoEx, with OpenInputDesktop as a second
                          opinion). Jarvis runs inside your signed-in session, so while it answers, a session
                          exists; before anyone has signed in after a boot, Jarvis isn't running at all and the
                          phone can only see whether the PC answers on the network.

States (the phone adds OFFLINE / WAKING / BOOTING itself, from what it can see on the network):

    OFFLINE -> WAKING -> BOOTING -> JARVIS_STARTING -> ONLINE_LOCKED -> (AUTHENTICATION_AVAILABLE) -> READY
    plus SLEEPING / HIBERNATING / SHUTTING_DOWN (announced by the PC just before), UNKNOWN.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

STATES = ("OFFLINE", "WAKING", "BOOTING", "JARVIS_STARTING", "ONLINE_LOCKED", "AUTHENTICATION_AVAILABLE",
          "AUTHENTICATED", "READY", "SLEEPING", "HIBERNATING", "SHUTTING_DOWN", "UNKNOWN")
POWER_STATES = {"sleep": "SLEEPING", "hibernate": "HIBERNATING", "shutdown": "SHUTTING_DOWN",
                "restart": "SHUTTING_DOWN"}
POWER_ANNOUNCE_SECONDS = 90

WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION = 0, 0xFFFFFFFF
WTS_CLIENT_PROTOCOL_TYPE, WTS_SESSION_INFO_EX = 16, 25
WTS_SESSIONSTATE_LOCK, WTS_SESSIONSTATE_UNLOCK = 0, 1
WTS_ACTIVE = 0


@dataclass(frozen=True)
class Session:
    locked: bool | None      # None: couldn't tell
    remote: bool             # a Remote Desktop session (not the PC's own screen)
    active: bool | None      # connected (WTSActive) vs disconnected
    source: str              # which check answered

    @property
    def windows(self) -> str:
        return {True: "locked", False: "active", None: "unknown"}[self.locked]


class WindowsSessionProbe:
    """Real Windows calls; tests replace it."""

    def session_info(self) -> tuple[int, int] | None:
        """(connect state, session flags) of this process's session, or None."""
        wtsapi = ctypes.WinDLL("wtsapi32.dll")
        buffer, size = ctypes.c_void_p(), ctypes.c_uint32()
        if not wtsapi.WTSQuerySessionInformationW(ctypes.c_void_p(WTS_CURRENT_SERVER_HANDLE),
                                                  ctypes.c_uint32(WTS_CURRENT_SESSION), WTS_SESSION_INFO_EX,
                                                  ctypes.byref(buffer), ctypes.byref(size)):
            return None
        try:
            if size.value < 20:
                return None
            # WTSINFOEXW: DWORD Level; then (8-byte aligned) WTSINFOEX_LEVEL1_W: SessionId, SessionState, SessionFlags
            words = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int32))
            if words[0] != 1:
                return None
            return int(words[3]), int(words[4])
        finally:
            wtsapi.WTSFreeMemory(buffer)

    def protocol(self) -> int | None:
        wtsapi = ctypes.WinDLL("wtsapi32.dll")
        buffer, size = ctypes.c_void_p(), ctypes.c_uint32()
        if not wtsapi.WTSQuerySessionInformationW(ctypes.c_void_p(WTS_CURRENT_SERVER_HANDLE),
                                                  ctypes.c_uint32(WTS_CURRENT_SESSION), WTS_CLIENT_PROTOCOL_TYPE,
                                                  ctypes.byref(buffer), ctypes.byref(size)):
            return None
        try:
            return int(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint16))[0])
        finally:
            wtsapi.WTSFreeMemory(buffer)

    def input_desktop_open(self) -> bool:
        """False while the secure (lock / sign-in) desktop has the input."""
        user32 = ctypes.WinDLL("user32.dll")
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        handle = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not handle:
            return False
        user32.CloseDesktop(ctypes.c_void_p(handle))
        return True


def session(probe: Any = None) -> Session:
    if probe is None:
        if sys.platform != "win32":
            return Session(None, False, None, "not Windows")
        probe = WindowsSessionProbe()
    remote = False
    try:
        remote = probe.protocol() == 2  # WTS_PROTOCOL_TYPE_RDP
    except Exception:
        pass
    info = None
    try:
        info = probe.session_info()
    except Exception:
        info = None
    active = None
    if info is not None:
        state, flags = info
        active = state == WTS_ACTIVE
        if flags in (WTS_SESSIONSTATE_LOCK, WTS_SESSIONSTATE_UNLOCK):
            locked = flags == WTS_SESSIONSTATE_LOCK
            if not active and not remote:
                locked = True  # a disconnected console session has no one at it: treat as locked
            return Session(locked, remote, active, "WTSSessionInfoEx")
    try:
        return Session(not probe.input_desktop_open(), remote, active, "input desktop")
    except Exception:
        return Session(None, remote, active, "unavailable")


# ------------------------------------------------------------------ announced power changes

_POWER: dict[str, Any] = {}
_LOCK = threading.Lock()


def announce_power(action: str, clock=time.time) -> None:
    """Called right before Jarvis sleeps / hibernates / shuts down / restarts the PC (so the phone can say why it
    went quiet instead of 'offline, reason unknown')."""
    if action in POWER_STATES:
        with _LOCK:
            _POWER.update(action=action, at=clock())


def announced_power(clock=time.time) -> dict[str, Any] | None:
    with _LOCK:
        if _POWER and clock() - _POWER["at"] <= POWER_ANNOUNCE_SECONDS:
            return dict(_POWER)
    return None


def reset() -> None:
    with _LOCK:
        _POWER.clear()


def snapshot(*, jarvis_ready: bool, session_state: Session, auth: Any = None, clock=time.time) -> dict[str, Any]:
    """What the phone shows. ``auth``: app.remote.win_auth.AuthCapabilities (optional)."""
    power = announced_power(clock)
    rd = getattr(auth, "remote_desktop", "unknown") if auth is not None else "unknown"
    base = {"windows": session_state.windows, "remote_session": session_state.remote,
            "windows_source": session_state.source, "remote_desktop": rd}
    if power:
        return {**base, "state": POWER_STATES[power["action"]],
                "explain": f"The PC is going to {power['action']} (asked {int(clock() - power['at'])} s ago)."}
    if not jarvis_ready:
        return {**base, "state": "JARVIS_STARTING", "explain": "Jarvis is starting on the PC."}
    if session_state.locked is True:
        state = "AUTHENTICATION_AVAILABLE" if rd == "available" else "ONLINE_LOCKED"
        explain = ("Windows is locked. Remote Desktop is available: sign in there with your Windows account."
                   if rd == "available" else
                   "Windows is locked. Windows requires sign-in at the PC" +
                   (" (Remote Desktop is unavailable on this edition/configuration)." if rd in
                    ("unsupported_edition", "disabled") else ".") + " Jarvis cannot bypass the lock screen.")
        return {**base, "state": state, "explain": explain}
    if session_state.locked is False:
        where = "a Remote Desktop session" if session_state.remote else "the PC's screen"
        return {**base, "state": "READY", "explain": f"Your Windows session is active on {where}."}
    return {**base, "state": "UNKNOWN", "explain": "Jarvis couldn't read whether Windows is locked."}
