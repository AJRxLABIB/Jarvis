"""3.10.0 Phase 7: device registry, detected capabilities, Wake-on-LAN with honest verification, remote status."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import devices
from app.memory.database import Database
from app.security.permissions import PermissionEngine
from app.tools import winapi
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from tests.test_tools import ScriptedConfirmer


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "j.db")
    database.initialize()
    return database


def test_magic_packet_and_mac_checks() -> None:
    packet = devices.magic_packet("3c-52-82-1a-2b-3c")
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6 and packet[6:12] == bytes.fromhex("3C52821A2B3C")
    assert devices.normalize_mac("3C52821A2B3C") == "3C:52:82:1A:2B:3C"
    for bad in ("hello", "00:00:00:00:00:00", "FF-FF-FF-FF-FF-FF", "3C:52:82"):
        with pytest.raises(ToolError):
            devices.normalize_mac(bad)
    sent = []
    assert devices.send_magic_packet("3C:52:82:1A:2B:3C", ("255.255.255.255", "192.168.1.255"),
                                     sender=lambda p, a: sent.append(a)) == 6
    assert ("192.168.1.255", 9) in sent and devices.subnet_broadcast("192.168.1.40") == "192.168.1.255"


def fake_windows(monkeypatch, adapters, fast=False, states="The following sleep states are available on this "
                 "system:\n    Standby (S3)\n    Hibernate\nThe following sleep states are not available"):
    monkeypatch.setattr(winapi, "is_windows", lambda: True)
    monkeypatch.setattr(winapi, "adapter_wake_settings", lambda: adapters)
    monkeypatch.setattr(winapi, "fast_startup_enabled", lambda: fast)
    monkeypatch.setattr(winapi, "power_states_text", lambda: states)


def caps_of(monkeypatch, **kw):
    fake_windows(monkeypatch, **kw)
    info, caps = devices.detect_self()
    return info, {c.name: c for c in caps}


def test_wifi_only_laptop_is_honest(monkeypatch) -> None:
    info, caps = caps_of(monkeypatch, adapters=[{"Name": "Wi-Fi", "Media": "Native 802.11", "Desc": "Intel Wi-Fi 6",
                                                  "Mac": "AA-BB-CC-DD-EE-01", "Magic": "Enabled"}], fast=True)
    assert caps["wake_from_sleep"].status == "supported"
    assert caps["wake_from_shutdown"].status == "unsupported" and "no wired" in caps["wake_from_shutdown"].detail
    assert caps["hibernate"].status == "supported" and info["mac"] == "AA:BB:CC:DD:EE:01"
    setup = devices.wake_setup(list(caps.values()), info)
    assert "can't wake itself" in setup and "never open a port" in setup


def test_wired_laptop_is_at_most_unknown(monkeypatch) -> None:
    _, caps = caps_of(monkeypatch, adapters=[{"Name": "Ethernet", "Media": "802.3", "Desc": "Realtek PCIe GbE",
                                              "Mac": "AA-BB-CC-DD-EE-02", "Magic": "Enabled"}], fast=True,
                      states="The following sleep states are available on this system:\n Standby\nThe following "
                             "sleep states are not available on this system:\n Hibernate")
    shutdown = caps["wake_from_shutdown"]
    assert shutdown.status == "unknown" and "Fast startup is on" in shutdown.detail and "BIOS" in shutdown.detail
    assert caps["hibernate"].status == "unsupported"


def test_registry_keeps_a_verified_wake(db, monkeypatch) -> None:
    fake_windows(monkeypatch, [{"Name": "Ethernet", "Media": "802.3", "Mac": "AA-BB-CC-DD-EE-03", "Magic": "Enabled"}])
    registry = devices.DeviceRegistry(db)
    me = registry.register_self()
    assert me.is_self and me.last_state == "online"
    registry.set_capability(me, "wake_from_shutdown", "verified", "it worked")
    again = registry.register_self()  # re-detection never downgrades what really worked
    assert {c.name: c.status for c in again.capabilities}["wake_from_shutdown"] == "verified"
    with pytest.raises(ToolError):
        registry.remove(me)


def test_wake_is_only_reported_when_the_device_answers(db) -> None:
    registry = devices.DeviceRegistry(db)
    desktop = registry.add("desktop", "3C-52-82-1A-2B-3C", "192.168.1.50")
    answers = iter([None, None, None, 445])
    sent = []
    now = [0.0]
    report = devices.wake(desktop, registry, wait=60, probe=lambda a, p=devices.PROBE_PORTS: next(answers),
                          sleep=lambda s: now.__setitem__(0, now[0] + s), sender=lambda p, a: sent.append(a),
                          clock=lambda: now[0])
    assert report.online and report.stages[0].startswith("Wake request sent") and "is on" in report.stages[1]
    caps = {c.name: c.status for c in registry.find("desktop").capabilities}
    assert caps["wake_on_lan"] == "verified" and registry.find("desktop").last_state == "online"


def test_a_wake_that_didnt_work_says_so(db) -> None:
    registry = devices.DeviceRegistry(db)
    desktop = registry.add("desktop", "3C-52-82-1A-2B-3C", "192.168.1.50")
    now = [0.0]
    report = devices.wake(desktop, registry, wait=30, probe=lambda a, p=devices.PROBE_PORTS: None,
                          sleep=lambda s: now.__setitem__(0, now[0] + s), sender=lambda p, a: None, clock=lambda: now[0])
    assert not report.online and "couldn't confirm" in report.stages[-1]
    assert {c.name: c.status for c in registry.find("desktop").capabilities}["wake_on_lan"] == "unknown"


def test_without_an_address_it_cant_verify(db) -> None:
    registry = devices.DeviceRegistry(db)
    tv = registry.add("tv", "3C-52-82-1A-2B-3D")
    report = devices.wake(tv, registry, sender=lambda p, a: None)
    assert not report.online and "can't check whether it woke up" in report.stages[-1]


def test_this_pc_cannot_wake_itself(db, monkeypatch) -> None:
    fake_windows(monkeypatch, [])
    registry = devices.DeviceRegistry(db)
    with pytest.raises(ToolError, match="already on"):
        devices.wake(registry.register_self(), registry)


def test_device_tools(db, monkeypatch) -> None:
    fake_windows(monkeypatch, [{"Name": "Wi-Fi", "Media": "802.11", "Mac": "AA-BB-CC-DD-EE-04", "Magic": "Disabled"}])
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ToolContext(Settings(), database=db),
                      confirmer=ScriptedConfirmer(True))

    def go(tool_name, **arguments):
        return asyncio.run(ex.execute(ToolCall(tool_name, arguments))).result

    info = go("device_info")
    assert info.ok and "wake from sleep: unsupported" in info.message and "Wake on Magic Packet" in info.message
    assert go("add_device", name="desktop", mac="3C-52-82-1A-2B-3C", address="192.168.1.50").ok
    assert "desktop (computer)" in go("list_devices").message
    assert go("remove_device", name="desktop").ok


def test_hibernate_only_when_windows_offers_it(monkeypatch) -> None:
    from app.tools.windows import PowerArgs, power_control

    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "power_states_text", lambda: "The following sleep states are available on this "
                        "system:\n Standby\nThe following sleep states are not available on this system:\n Hibernate")
    with pytest.raises(ToolError, match="isn't available"):
        power_control(PowerArgs(action="hibernate"), ToolContext(Settings()))


def test_phone_status(tmp_path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    controller._wake_summary = "from sleep supported, from shutdown unknown"
    status = controller.remote_status()
    assert status["online"] and status["version"] and status["wake"].startswith("from sleep")
    from app.remote.server import PAGE

    assert "PC not reachable" in PAGE and "remote wake" in PAGE


@pytest.mark.parametrize("said, tool, arguments", [
    ("can you turn on my laptop remotely", "device_info", {}),
    ("how can I wake this PC remotely", "device_info", {}),
    ("my devices", "list_devices", {}),
    ("wake my desktop", "wake_device", {"name": "desktop"}),
    ("is my desktop on", "check_device", {"name": "desktop"}),
    ("hibernate", "power_control", {"action": "hibernate"}),
])
def test_commands(said, tool, arguments) -> None:
    from app.agent.shortcuts import match_shortcut

    call = match_shortcut(said, lambda n: None, known_computer=lambda n: n.lower() == "desktop")
    assert call is not None and (call.name, dict(call.arguments)) == (tool, arguments)
    assert match_shortcut("turn on my kettle", lambda n: None, known_computer=lambda n: False) is None or \
        match_shortcut("turn on my kettle", lambda n: None, known_computer=lambda n: False).name != "wake_device"
