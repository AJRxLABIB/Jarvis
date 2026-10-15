"""OFFLINE_MODE=auto: online normally, internet features pause only while the connection is down."""

from __future__ import annotations

import asyncio

import pytest

from app.config.settings import Settings
from app.utils import network


def test_setting_values_and_old_values() -> None:
    assert Settings().offline_mode == "auto"
    assert Settings(offline_mode=True).offline_mode == "on" and Settings(offline_mode=True).forced_offline
    assert Settings(offline_mode="false").offline_mode == "auto"  # old "false" meant "online"
    assert Settings(offline_mode="OFF").offline_mode == "off"
    with pytest.raises(ValueError):
        Settings(offline_mode="sometimes")


def test_reasons(monkeypatch) -> None:
    status = network.NetworkStatus(check=lambda: False)
    monkeypatch.setattr(network, "MONITOR", status)
    assert network.offline_reason(Settings()) == network.NO_INTERNET_MESSAGE
    assert network.offline_reason(Settings(offline_mode="off")) is None
    assert network.offline_reason(Settings(offline_mode="on")) == network.FORCED_MESSAGE
    status.set(True)
    assert network.offline_reason(Settings()) is None


def test_cached_and_rechecked(monkeypatch) -> None:
    calls = []
    status = network.NetworkStatus(check=lambda: calls.append(1) or True)
    assert status.last_known() is None and status.online() and status.online()
    assert len(calls) == 1  # cached
    monkeypatch.setattr(network, "ONLINE_TTL", -1)
    status.online()
    assert len(calls) == 2


def test_connection_comes_back(monkeypatch) -> None:
    state = {"up": False}
    status = network.NetworkStatus(check=lambda: state["up"])
    monkeypatch.setattr(network, "MONITOR", status)
    monkeypatch.setattr(network, "OFFLINE_TTL", -1)  # re-check every time while offline
    assert network.offline_reason(Settings()) is not None
    state["up"] = True
    assert network.offline_reason(Settings()) is None


def test_is_offline_now_never_blocks(monkeypatch) -> None:
    status = network.NetworkStatus(check=lambda: pytest.fail("must not probe"))
    monkeypatch.setattr(network, "MONITOR", status)
    assert not network.is_offline_now(Settings())  # never checked: assume online
    status.set(False)
    assert network.is_offline_now(Settings()) and not network.is_offline_now(Settings(offline_mode="off"))


def test_network_tools_pause_only_without_internet(monkeypatch) -> None:
    from app.agent.tool_manager import ToolExecutor
    from app.ai.base import ToolCall
    from app.config.files import PermissionsConfig
    from app.security.permissions import PermissionEngine
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry

    status = network.NetworkStatus(check=lambda: False)
    monkeypatch.setattr(network, "MONITOR", status)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ToolContext(Settings()))
    result = asyncio.run(ex.execute(ToolCall("web_search", {"query": "python"}))).result
    assert result.error == "offline" and "no internet connection" in result.message
    local = asyncio.run(ex.execute(ToolCall("get_datetime", {}))).result
    assert local.ok  # everything local keeps working


def test_probe_uses_plain_tcp(monkeypatch) -> None:
    tried = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def connect(address, timeout):
        tried.append(address)
        if address[0] == "1.1.1.1":
            raise OSError("blocked")
        return Conn()

    monkeypatch.setattr(network, "_windows_says_disconnected", lambda: False)
    monkeypatch.setattr(network.socket, "create_connection", connect)
    assert network.probe() and tried == [("1.1.1.1", 443), ("8.8.8.8", 443)]
    monkeypatch.setattr(network, "_windows_says_disconnected", lambda: True)
    tried.clear()
    assert not network.probe() and tried == []
