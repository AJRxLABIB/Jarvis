"""Local smart-home devices (Shelly, Tasmota, WLED): local addresses only, and success only on the device's read-back.
All HTTP is simulated here (httpx.MockTransport) - not a test against real hardware."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.core import local_home
from app.core.local_home import LocalDevice, LocalDeviceError, LocalHome, check_address


class FakeDevices:
    def __init__(self, sticky: bool = False) -> None:
        self.on, self.bri, self.sticky, self.calls = False, 128, sticky, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if not self.sticky:
            if "turn=on" in url or "on=true" in url or "Power%20On" in url or "Power On" in url:
                self.on = True
            if "turn=off" in url or "on=false" in url or "Power%20Off" in url or "Power Off" in url:
                self.on = False
            if request.method == "POST":
                body = json.loads(request.content)
                self.on = body.get("on", self.on)
                self.bri = body.get("bri", self.bri)
        if "/relay/" in url:
            return httpx.Response(200, json={"ison": self.on})
        if "/rpc/" in url:
            return httpx.Response(200, json={"output": self.on})
        if "/cm?" in url:
            return httpx.Response(200, json={"POWER": "ON" if self.on else "OFF"})
        return httpx.Response(200, json={"on": self.on, "bri": self.bri})


@pytest.fixture()
def fake(monkeypatch):
    devices = FakeDevices()
    monkeypatch.setattr(local_home, "TRANSPORT", httpx.MockTransport(devices.handler))
    return devices


def test_only_local_addresses() -> None:
    assert check_address("192.168.1.50") == "192.168.1.50" and check_address("10.0.0.7:8080")
    assert check_address("Shelly-Plug.local") == "shelly-plug.local"
    for bad in ("8.8.8.8", "example.com", "127.0.0.1", "0.0.0.0", "192.168.1.5:99999", "http://192.168.1.5"):
        with pytest.raises(LocalDeviceError):
            check_address(bad)


@pytest.mark.parametrize("kind", ["shelly", "shelly2", "tasmota", "wled"])
def test_switch_and_verify_every_kind(kind, fake) -> None:
    device = LocalDevice("lamp", kind, "192.168.1.50")
    outcome = local_home.set_state(device, on=True)
    assert outcome["verified"] and outcome["state"]["on"] is True
    assert local_home.set_state(device, on=False)["state"]["on"] is False


def test_a_device_that_does_not_change_is_not_reported_as_done(monkeypatch) -> None:
    stuck = FakeDevices(sticky=True)
    monkeypatch.setattr(local_home, "TRANSPORT", httpx.MockTransport(stuck.handler))
    assert local_home.set_state(LocalDevice("heater", "shelly", "192.168.1.9"), on=True)["verified"] is False


def test_registry_and_tool(tmp_path: Path, fake) -> None:
    from app.security.permissions import RiskLevel
    from app.tools.base import ToolError
    from app.tools.local_home_tool import LocalDeviceArgs, _risk, local_device

    home = LocalHome(tmp_path)
    home.add(LocalDevice("desk light", "wled", "192.168.1.60", confirm=False))
    home.add(LocalDevice("heater plug", "shelly", "192.168.1.61"))
    with pytest.raises(LocalDeviceError):
        home.add(LocalDevice("cloud", "shelly", "52.1.2.3"))
    ctx = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path))
    assert local_device(LocalDeviceArgs(action="brightness", device="desk", brightness=50), ctx).ok
    assert fake.bri == 127 and fake.on                              # 50 % of 255, read back from the device
    assert "on" in local_device(LocalDeviceArgs(action="status", device="desk light"), ctx).message
    assert _risk(LocalDeviceArgs(action="on", device="heater plug"), ctx) == RiskLevel.MODERATE   # relays ask
    assert _risk(LocalDeviceArgs(action="on", device="desk light"), ctx) == RiskLevel.SAFE
    with pytest.raises(ToolError):
        local_device(LocalDeviceArgs(action="brightness", device="heater plug", brightness=10), ctx)
    with pytest.raises(ToolError):
        local_device(LocalDeviceArgs(action="on", device="garage"), ctx)
    assert home.remove("desk light") and not home.remove("desk light")
    (tmp_path / "home_devices.json").write_text('[{"name": "x", "kind": "shelly", "address": "8.8.8.8"}]', "utf-8")
    assert home.devices() == []                                    # a hand-edited internet address is never used


def test_home_assistant_change_is_only_claimed_when_ha_reports_it(monkeypatch) -> None:
    from app.config.settings import Settings
    from app.tools import smart_home
    from app.tools.base import ToolContext

    states = [{"entity_id": "switch.heater", "state": "off", "attributes": {"friendly_name": "Heater"}}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/states":
            return httpx.Response(200, json=states)
        if request.url.path.startswith("/api/states/"):
            return httpx.Response(200, json={"state": "off"})           # the device never switched
        return httpx.Response(200, json=[])

    monkeypatch.setattr(smart_home, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(smart_home.time, "sleep", lambda s: None)
    smart_home._CACHE.clear()
    ctx = ToolContext(Settings(home_assistant_url="http://192.168.1.10:8123", home_assistant_token="t"))
    result = smart_home.home_control(smart_home.HomeArgs(device="heater", action="on"), ctx)
    assert not result.ok and result.error == "not_verified" and "still reports 'off'" in result.message
    smart_home._CACHE.clear()
