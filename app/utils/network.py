"""Is the internet reachable? Used for OFFLINE_MODE=auto (the default).

auto: internet features work normally and turn off by themselves only while
there is no connection (they come back automatically). on: always offline.
off: never check.

The check is local and quick: Windows' own "is any network connected" flag,
then a plain TCP connection to well-known public DNS servers (no data is sent,
nothing is requested). The result is cached so it isn't repeated for every
request.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config.settings import Settings

PROBES = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443), ("208.67.222.222", 443))
PROBE_TIMEOUT = 1.5
ONLINE_TTL = 60.0  # re-check a working connection once a minute
OFFLINE_TTL = 8.0  # while offline, re-check often so features come back quickly
FORCED_MESSAGE = "Offline mode is on (OFFLINE_MODE=on), so internet features are unavailable."
NO_INTERNET_MESSAGE = ("There's no internet connection right now, so I can't do that. "
                       "Internet features come back by themselves when the connection does.")


def _windows_says_disconnected() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        flags = ctypes.c_ulong(0)
        return not ctypes.windll.wininet.InternetGetConnectedState(ctypes.byref(flags), 0)
    except Exception:
        return False


def probe() -> bool:
    """True if a public server answers a TCP connection."""
    if _windows_says_disconnected():
        return False
    for host, port in PROBES:
        try:
            with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
                return True
        except OSError:
            continue
    return False


class NetworkStatus:
    def __init__(self, check=None) -> None:
        self._check = check or (lambda: probe())
        self._lock = threading.Lock()
        self._online: bool | None = None
        self._checked = 0.0
        self._refreshing = False

    def _stale(self) -> bool:
        if self._online is None:
            return True
        ttl = ONLINE_TTL if self._online else OFFLINE_TTL
        return time.monotonic() - self._checked > ttl

    def online(self, refresh: bool = False) -> bool:
        """Current status; checks (blocking, at most a few seconds) when the cached result is old."""
        if refresh or self._stale():
            value = bool(self._check())
            with self._lock:
                self._online, self._checked = value, time.monotonic()
        return bool(self._online)

    def last_known(self) -> bool | None:
        """Cached status without checking (None = never checked)."""
        return self._online

    def refresh_in_background(self) -> None:
        with self._lock:
            if self._refreshing or not self._stale():
                return
            self._refreshing = True

        def run() -> None:
            try:
                self.online(refresh=True)
            finally:
                self._refreshing = False

        threading.Thread(target=run, name="network-check", daemon=True).start()

    def set(self, online: bool) -> None:
        """For tests and diagnostics."""
        with self._lock:
            self._online, self._checked = online, time.monotonic()


MONITOR = NetworkStatus()


def offline_reason(settings: "Settings") -> str | None:
    """Why internet features can't be used right now, or None if they can (may check the connection)."""
    if settings.offline_mode == "on":
        return FORCED_MESSAGE
    if settings.offline_mode == "off":
        return None
    return None if MONITOR.online() else NO_INTERNET_MESSAGE


def is_offline_now(settings: "Settings") -> bool:
    """Non-blocking: forced offline, or the last check found no connection."""
    if settings.offline_mode == "on":
        return True
    return settings.offline_mode == "auto" and MONITOR.last_known() is False
