"""Strict offline mode, enforced where it cannot be forgotten: at the socket.

With ``OFFLINE_MODE=on`` every attempt to open a connection (or send a datagram) to an address outside this computer and
your own network is refused here, before any packet leaves - whichever library asked (httpx, aiohttp, urllib, a voice
engine, a browser helper). Allowed: loopback, private ranges (10/8, 172.16/12, 192.168/16, fc00::/7), link-local, local-network multicast (224.0.0.0/24, ff02::/16: device discovery) and names
that can only be local (``localhost``, single-label names, ``*.local``, ``*.lan``, ``*.home.arpa``, ``*.internal``).
A hostname that is anything else is refused WITHOUT being looked up, so not even a DNS query leaks.

The refusal is an ``OfflineViolation`` (an ``OSError``), so callers see an ordinary network failure and degrade as they
already do; each block is counted and the host (never the payload) is remembered for the Security view and the log.
``install`` is idempotent and the strictness is read on every call, so switching the setting takes effect at once.

Limits: it covers Python code in this process. A web PROXY configured on this PC or network is a local address, so a library
that honours proxy settings would reach it - and the proxy could forward the request. ``status()`` therefore reports any proxy
variables that are set (``proxy``), and Jarvis's own clients ignore system proxies for local traffic. A separate program Jarvis launches (a browser, Ollama, a script) is not
inside this guard; Ollama is local, and tools that start other programs ask first. Raw sockets created through
``ctypes`` or an extension module bypass it.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

_LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain")


class OfflineViolation(OSError):
    """Strict offline mode refused a connection to somewhere outside this computer and your network."""


_lock = threading.Lock()
_strict: Callable[[], bool] = lambda: False
_originals: dict[str, Any] = {}
_blocked: deque[str] = deque(maxlen=50)
_count = 0
_on_block: Callable[[str], None] | None = None


def local_address(host: str) -> bool:
    """True when ``host`` can only be on this computer or your own network."""
    host = (host or "").strip().strip("[]").lower().rstrip(".")
    if not host:
        return True                                   # an empty host is the wildcard / local socket
    host = host.split("%", 1)[0]                      # IPv6 zone id
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES)
    if ip.is_multicast:                               # only the link-local scope (mDNS 224.0.0.251, ff02::fb): it never leaves the local network
        return ip in ipaddress.ip_network("224.0.0.0/24") or ip in ipaddress.ip_network("ff02::/16")
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified)


def _check(host: Any, where: str) -> None:
    global _count
    if not _strict():
        return
    if isinstance(host, bytes):
        host = host.decode("idna", "ignore")
    if host is None or local_address(str(host)):
        return
    with _lock:
        _count += 1
        _blocked.append(str(host)[:80])
    if _on_block is not None:
        try:
            _on_block(str(host)[:80])
        except Exception:
            pass
    raise OfflineViolation(f"Offline mode is on: {where} to {str(host)[:80]} was refused (it is outside this computer and your network).")


def _address_host(address: Any) -> Any:
    return address[0] if isinstance(address, tuple) and address else None


def install(strict: Callable[[], bool], on_block: Callable[[str], None] | None = None) -> None:
    """Start enforcing. ``strict`` is asked on every connection, so it can follow a setting that changes while running."""
    global _strict, _on_block
    _strict, _on_block = strict, on_block
    if _originals:
        return
    _originals["connect"] = socket.socket.connect
    _originals["connect_ex"] = socket.socket.connect_ex
    _originals["sendto"] = socket.socket.sendto
    _originals["getaddrinfo"] = socket.getaddrinfo
    _originals["gethostbyname"] = socket.gethostbyname

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check(_address_host(address), "a connection")
        return _originals["connect"](self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check(_address_host(address), "a connection")
        return _originals["connect_ex"](self, address)

    def sendto(self, data, *args):
        if self.family in (socket.AF_INET, socket.AF_INET6) and args:
            _check(_address_host(args[-1]), "a datagram")
        return _originals["sendto"](self, data, *args)

    def getaddrinfo(host, *args, **kwargs):
        _check(host, "a name lookup")
        return _originals["getaddrinfo"](host, *args, **kwargs)

    def gethostbyname(host):
        _check(host, "a name lookup")
        return _originals["gethostbyname"](host)

    socket.socket.connect = connect                    # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex              # type: ignore[method-assign]
    socket.socket.sendto = sendto                      # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo                   # type: ignore[assignment]
    socket.gethostbyname = gethostbyname               # type: ignore[assignment]


def uninstall() -> None:
    """Restore the real functions (tests only)."""
    global _strict, _on_block
    if _originals:
        socket.socket.connect = _originals["connect"]              # type: ignore[method-assign]
        socket.socket.connect_ex = _originals["connect_ex"]        # type: ignore[method-assign]
        socket.socket.sendto = _originals["sendto"]                # type: ignore[method-assign]
        socket.getaddrinfo = _originals["getaddrinfo"]
        socket.gethostbyname = _originals["gethostbyname"]
        _originals.clear()
    _strict, _on_block = (lambda: False), None


def proxies() -> list[str]:
    """Proxy environment variables that are set. The guard cannot see past a proxy, so strict mode should warn about them."""
    import os

    found = [k for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy") if os.environ.get(k)]
    return sorted({k.upper() for k in found}) if os.name == "nt" else sorted(found)      # Windows variable names are case-insensitive: one entry, not two


def status() -> dict[str, Any]:
    return {"installed": bool(_originals), "strict": bool(_originals) and _strict(), "blocked": _count,
            "recent": list(_blocked)[-5:], "proxy": proxies()}
