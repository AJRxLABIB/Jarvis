"""Strict offline mode at the socket: nothing leaves this computer / your network, and not even a DNS query does."""

from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.request

import pytest

from app.utils import egress


@pytest.fixture()
def guard():
    state = {"strict": True}
    blocked: list[str] = []
    egress.install(lambda: state["strict"], blocked.append)
    yield state, blocked
    egress.uninstall()


@pytest.mark.parametrize("host,local", [
    ("127.0.0.1", True), ("::1", True), ("localhost", True), ("192.168.1.20", True), ("10.0.0.5", True),
    ("172.16.4.2", True), ("172.32.0.1", False), ("169.254.1.1", True), ("fe80::1%eth0", True), ("fd00::1", True),
    ("mydesktop", True), ("phone.local", True), ("nas.lan", True), ("router.home.arpa", True), ("", True),
    ("8.8.8.8", False), ("93.184.216.34", False), ("example.com", False), ("api.openai.com", False),
    ("localhost.evil.com", False), ("2606:4700::1111", False)])
def test_what_counts_as_local(host, local):
    assert egress.local_address(host) is local


def test_public_connections_are_refused_before_any_packet(guard):
    for target in ("93.184.216.34", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(egress.OfflineViolation):
            s.connect((target, 80))
        with pytest.raises(egress.OfflineViolation):
            s.connect_ex((target, 80))
        s.close()
    with pytest.raises(egress.OfflineViolation):
        socket.create_connection(("example.com", 443), timeout=1)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with pytest.raises(egress.OfflineViolation):
        u.sendto(b"x", ("8.8.8.8", 53))
    u.close()


def test_names_are_refused_without_being_looked_up(guard, monkeypatch):
    looked_up = []
    real = egress._originals["getaddrinfo"]
    egress._originals["getaddrinfo"] = lambda host, *a, **k: looked_up.append(host) or real(host, *a, **k)
    try:
        with pytest.raises(egress.OfflineViolation):
            socket.getaddrinfo("example.com", 443)
        with pytest.raises(egress.OfflineViolation):
            socket.gethostbyname("example.com")
        assert looked_up == []                                  # no DNS query left the machine
        assert socket.getaddrinfo("localhost", 80)
        assert looked_up == ["localhost"]
    finally:
        egress._originals["getaddrinfo"] = real


def test_loopback_and_lan_still_work_in_strict_mode(guard):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    threading.Thread(target=lambda: server.accept()[0].close(), daemon=True).start()
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass
    server.close()


def test_libraries_see_an_ordinary_network_failure(guard):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # no proxy: the guard is what stops it
    with pytest.raises(urllib.error.URLError) as caught:
        opener.open("http://example.com/", timeout=2)
    assert "Offline mode is on" in str(caught.value)
    import httpx

    with pytest.raises(httpx.ConnectError) as caught2:
        httpx.get("http://example.com/", timeout=2, trust_env=False)
    assert "Offline mode is on" in str(caught2.value)


def test_a_configured_proxy_is_reported_because_the_guard_cannot_see_past_it(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    assert egress.proxies() == []
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    assert egress.proxies() == ["HTTPS_PROXY"] and egress.status()["proxy"] == ["HTTPS_PROXY"]


def test_switching_the_setting_takes_effect_at_once_and_is_counted(guard):
    state, blocked = guard
    before = egress.status()["blocked"]
    with pytest.raises(egress.OfflineViolation):
        socket.getaddrinfo("example.com", 80)
    assert blocked == ["example.com"] and egress.status()["blocked"] == before + 1
    state["strict"] = False
    assert egress.status()["strict"] is False
    assert egress._originals                                   # still installed; only the setting changed
    state["strict"] = True
    assert egress.status()["strict"] is True and "example.com" in egress.status()["recent"]


def test_install_is_idempotent_and_uninstall_restores():
    orig = socket.getaddrinfo
    egress.install(lambda: True)
    patched = socket.getaddrinfo
    egress.install(lambda: True)
    assert socket.getaddrinfo is patched and patched is not orig
    egress.uninstall()
    assert socket.getaddrinfo is orig and not egress.status()["installed"]


def test_bootstrap_installs_the_guard_following_the_live_setting(tmp_path):
    from app.main import bootstrap

    ctx = bootstrap(tmp_path)
    try:
        assert egress.status()["installed"] and not egress.status()["strict"]
        ctx.settings = ctx.settings.model_copy(update={"offline_mode": "on"})
        assert egress.status()["strict"]
        with pytest.raises(egress.OfflineViolation):
            socket.getaddrinfo("example.com", 80)
    finally:
        egress.uninstall()


def test_security_view_states_the_real_network_mode(tmp_path):
    from types import SimpleNamespace

    from app.ui.command_center import security

    egress.install(lambda: True)
    try:
        strict = security.network_mode(SimpleNamespace(offline_mode="on"))
        assert any("Strict offline mode is ON" in i.text for i in strict.items)
        auto = security.network_mode(SimpleNamespace(offline_mode="auto"))
        assert any("OFFLINE_MODE=on makes it strict" in i.text for i in auto.items)
    finally:
        egress.uninstall()
    broken = security.network_mode(SimpleNamespace(offline_mode="on"))
    assert any(i.level == "warn" and "not installed" in i.text for i in broken.items)
