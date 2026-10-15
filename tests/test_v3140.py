"""3.14.0: the Jarvis Wake Android app - served by the phone remote, set up with this PC's details."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app.config.settings import Settings
from app.memory.database import Database
from app.remote import server as remote

ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android" / "JarvisWake"


def _server():
    from app.remote.pairing import PairingStore

    config = {"name": "LAPTOP", "mac": "3C:52:82:1A:2B:3C", "ip": "192.168.1.5", "broadcast": "192.168.1.255",
              "port": "8765", "public": "", "public_port": "9", "away": "", "wake_status": "supported", "notes": "x"}
    hub = remote.RemoteHub(lambda t: None, lambda ok, rid: False, lambda: None, lambda: {},
                           wake_config=lambda: dict(config))
    pairing = PairingStore(None, encrypt=False)
    return remote.RemoteServer(hub, pairing, 0), pairing


def _call(server, pairing, paths, paired=True):
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        client = TestClient(TestServer(server.app()))
        await client.start_server()
        try:
            headers = {}
            if paired:
                code = pairing.new_code()
                data = await (await client.post("/api/pair", json={"code": code, "name": "Pixel"})).json()
                headers = {"X-Device": data["device"], "X-Key": data["key"]}
            out = []
            for path in paths:
                response = await client.get(path, headers=headers)
                out.append((response.status, response.headers.get("Content-Type", ""), await response.read()))
            return out
        finally:
            await client.close()

    return asyncio.run(go())


def test_wake_config_needs_a_paired_phone_and_gives_a_setup_link() -> None:
    server, pairing = _server()
    [(denied, _, _)] = _call(server, pairing, ["/api/wake-config"], paired=False)
    assert denied in (401, 403)
    [(status, _, body)] = _call(server, pairing, ["/api/wake-config"])
    import json

    data = json.loads(body)
    link = urlparse(data["link"])
    query = {k: v[0] for k, v in parse_qs(link.query).items()}
    assert status == 200 and link.scheme == "jarviswake" and link.netloc == "setup"
    assert query["mac"] == "3C:52:82:1A:2B:3C" and query["broadcast"] == "192.168.1.255" and "notes" not in query


def test_apk_download(monkeypatch, tmp_path: Path) -> None:
    # both /wake-app.apk (legacy link) and /mobile/app.apk now serve the combined JARVIS Mobile app
    from app.remote import apk

    server, pairing = _server()
    monkeypatch.setattr(apk, "apk_path", lambda: None)
    for path in ("/wake-app.apk", "/mobile/app.apk"):
        [(status, _, body)] = _call(server, pairing, [path], paired=False)
        assert status == 404 and b"build_android.ps1" in body
    built = tmp_path / "JarvisMobile.apk"
    built.write_bytes(b"PK\x03\x04 fake apk")
    monkeypatch.setattr(apk, "apk_path", lambda: built)
    for path in ("/wake-app.apk", "/mobile/app.apk"):
        [(status, kind, body)] = _call(server, pairing, [path], paired=False)
        assert status == 200 and kind == "application/vnd.android.package-archive" and body.startswith(b"PK")


def test_phone_page_offers_the_app() -> None:
    assert "/mobile/app.apk" in remote.PAGE and "wakeSetup()" in remote.PAGE


def test_config_comes_from_the_registered_pc(tmp_path: Path) -> None:
    from app.ui.controller_parts.remote import RemoteMixin

    database = Database(tmp_path / "j.db")
    database.initialize()
    with database.transaction() as conn:
        conn.execute("INSERT INTO devices (id, name, kind, mac, address, is_self, created_at) VALUES "
                     "('self', 'LAPTOP-1', 'laptop', '3C:52:82:1A:2B:3C', '192.168.1.5', 1, '2026-09-26')")
        conn.execute("INSERT INTO device_capabilities (device_id, capability, status, detail, checked_at) VALUES "
                     "('self', 'wake_from_sleep', 'supported', 'magic packet on for Ethernet', '2026-09-26')")
    fake = SimpleNamespace(ctx=SimpleNamespace(database=database, settings=Settings(
        wake_public_address="myhome.ddns.net", wake_public_port=4009)))
    config = RemoteMixin.wake_app_config(fake)
    assert config["mac"] == "3C:52:82:1A:2B:3C" and config["name"] == "LAPTOP-1"
    assert config["public"] == "myhome.ddns.net" and config["public_port"] == "4009"
    assert config["wake_status"] == "supported" and config["away"] == ""  # Tailscale off


def test_android_app_sources() -> None:
    manifest = (ANDROID / "AndroidManifest.xml").read_text()
    assert 'android:scheme="jarviswake"' in manifest and "android.permission.INTERNET" in manifest
    assert 'android:targetSdkVersion="34"' in manifest
    for density in ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi"):
        assert (ANDROID / "res" / f"mipmap-{density}" / "ic_launcher.png").is_file()
    activity = (ANDROID / "src/com/jarvis/wake/MainActivity.java").read_text()
    assert "Your PC is ON" in activity and "Wol.answers" in activity  # "on" only when Jarvis answered
    wol = (ANDROID / "src/com/jarvis/wake/Wol.java").read_text()
    assert "setBroadcast(true)" in wol and "new byte[102]" in wol


def test_android_build_uses_official_tools_only() -> None:
    script = (ROOT / "packaging" / "build_android.ps1").read_text()
    hosts = set(re.findall(r"https://([^/\"]+)/", script))
    assert hosts <= {"aka.ms", "dl.google.com", "developer.android.com"}, hosts
    assert "apksigner" in script and "verify" in script


def test_setting_defaults() -> None:
    settings = Settings()
    assert settings.wake_public_address == "" and settings.wake_public_port == 9
