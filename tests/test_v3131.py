"""3.13.1: brightness check (why the monitor refuses), Defender update with your admin permission (UAC), phone
setup (QR code, home screen, Tailscale opt-in)."""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from app.agent.shortcuts import match_shortcut
from app.remote import server as remote
from app.tools import winapi, windows
from tests.test_windows_tools import run_tool


@pytest.fixture()
def win(monkeypatch):
    monkeypatch.setattr(winapi, "is_windows", lambda: True)
    monkeypatch.setattr(winapi, "require_windows", lambda: None)


def test_brightness_check_explains_a_monitor_without_ddc(win, monkeypatch) -> None:
    monkeypatch.setattr(winapi, "brightness_report", lambda: {"laptop_screen": False, "monitors": [
        {"name": "Generic PnP Monitor", "ddc": False, "vcp": False, "capabilities": False}]})
    result = run_tool("brightness_control", action="check").result
    assert result.ok and not result.data["hardware"] and "doesn't answer" in result.message
    assert "DDC/CI -> On" in result.message and "DisplayPort or HDMI" in result.message


def test_brightness_check_when_the_monitor_works(win, monkeypatch) -> None:
    monkeypatch.setattr(winapi, "brightness_report", lambda: {"laptop_screen": False, "monitors": [
        {"name": "DELL U2419H", "ddc": False, "vcp": True, "capabilities": True}]})
    result = run_tool("brightness_control", action="check").result
    assert result.data["hardware"] and "takes brightness commands" in result.message and "DDC/CI ->" not in result.message


def test_check_brightness_shortcut() -> None:
    for text in ("check brightness", "why can't you change my monitor brightness"):
        assert match_shortcut(text, lambda n: None).arguments == {"action": "check"}


def test_software_dimming_points_to_the_check(win, monkeypatch) -> None:
    monkeypatch.setattr(winapi, "get_brightness", lambda: None)
    monkeypatch.setattr(winapi, "set_brightness", lambda p: 0)
    from app.agent.tool_manager import ToolExecutor
    from app.ai.base import ToolCall
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry

    context = ToolContext(Settings(), dim_screen=lambda level: True)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), context)
    result = asyncio.run(ex.execute(ToolCall("brightness_control", {"action": "set", "level": 30}))).result
    assert result.ok and "check brightness" in result.message and windows.SOFT["level"] == 30


class Defender:
    def __init__(self, monkeypatch, tmp_path, first_code=2, elevated_code=0, versions=("1.459.410.0", "1.459.500.0"),
                 admin=False):
        program = tmp_path / "MpCmdRun.exe"
        program.write_text("x")
        self.versions, self.elevated = list(versions), []
        monkeypatch.setattr(winapi, "_mpcmdrun", lambda: program)
        monkeypatch.setattr(winapi, "is_admin", lambda: admin)
        monkeypatch.setattr(winapi, "defender_signature_version", lambda: self.versions.pop(0) if self.versions else None)
        monkeypatch.setattr(winapi.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=first_code))
        monkeypatch.setattr(winapi, "run_elevated", lambda p, params, t: self.elevated.append(params) or elevated_code)


def test_defender_asks_windows_for_admin_permission_and_verifies(win, monkeypatch, tmp_path) -> None:
    defender = Defender(monkeypatch, tmp_path)
    done, detail = winapi.update_defender_signatures()
    assert done and defender.elevated == ["-SignatureUpdate"]
    assert "from 1.459.410.0 to 1.459.500.0" in detail and "administrator permission" in detail


def test_defender_permission_declined_is_said_plainly(win, monkeypatch, tmp_path) -> None:
    Defender(monkeypatch, tmp_path, elevated_code=None)
    done, detail = winapi.update_defender_signatures()
    assert not done and "wasn't accepted" in detail


def test_defender_without_elevation_needed(win, monkeypatch, tmp_path) -> None:
    defender = Defender(monkeypatch, tmp_path, first_code=0, versions=("1.2.3.4", "1.2.3.4"))
    done, detail = winapi.update_defender_signatures()
    assert done and "already the newest" in detail and not defender.elevated


def test_elevation_is_only_for_fixed_programs() -> None:
    import inspect

    source = inspect.getsource(winapi.update_defender_signatures)
    assert 'run_elevated(program, "-SignatureUpdate"' in source  # fixed argument, never user text
    assert subprocess  # (the module keeps shell=False everywhere - see test_security_static)


def test_tailscale_is_only_allowed_when_switched_on() -> None:
    assert not remote.is_private("100.101.102.103")
    assert remote.is_private("100.101.102.103", tailscale=True)
    assert not remote.is_private("8.8.8.8", tailscale=True) and remote.is_private("192.168.0.9")


def test_away_address_needs_the_setting(monkeypatch) -> None:
    monkeypatch.setattr(remote, "tailscale_address", lambda: "100.70.1.2")
    off = remote.RemoteServer(None, None, 8765)
    on = remote.RemoteServer(None, None, 8765, allow_tailscale=True)
    assert off.away_url is None and on.away_url == "http://100.70.1.2:8765/"


def test_home_screen_manifest_and_icon() -> None:
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from app.remote.pairing import PairingStore

    hub = remote.RemoteHub(lambda t: None, lambda ok, rid: False, lambda: None, lambda: {})
    server = remote.RemoteServer(hub, PairingStore(None, encrypt=False), 0)

    async def go():
        client = TestClient(TestServer(server.app()))
        await client.start_server()
        try:
            manifest = await (await client.get("/manifest.json")).json()
            icon = await client.get("/icon.svg")
            page = await (await client.get("/")).text()
            return manifest, icon.status, page
        finally:
            await client.close()

    manifest, icon, page = asyncio.run(go())
    assert manifest["display"] == "standalone" and icon == 200 and 'rel="manifest"' in page


def test_qr_code_for_the_phone() -> None:
    pytest.importorskip("qrcode")
    from app.ui.phone_setup import qr_matrix

    matrix = qr_matrix("https://192.168.1.5:8765/")
    assert matrix and len(matrix) == len(matrix[0]) > 20
