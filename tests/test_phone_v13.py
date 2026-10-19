"""JARVIS Mobile 1.3 from the PC side: files and clipboard from the phone, reading the phone's screen, tapping by
text, putting text on the phone's clipboard, locating the phone."""

from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from app.memory.database import Database
from app.remote import mobile
from app.remote.phone_inbox import InboxError, PhoneInbox, safe_name
from app.remote.phone_link import check_action
from app.security.permissions import RiskLevel
from tests.test_v3150 import paired

ANDROID = Path(__file__).resolve().parents[1] / "android" / "JarvisMobile"


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "x.db")
    database.initialize()
    return database


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_safe_names():
    assert safe_name("../../Windows/evil.exe") == "evil.exe"
    assert safe_name("C:\\x\\photo 1.jpg") == "photo 1.jpg"
    assert safe_name("") == "file" and safe_name("a<b>|c.txt") == "a_b_c.txt"
    assert safe_name("ছবি.jpg") == "ছবি.jpg"


def test_inbox_saves_never_overwrites_and_limits(tmp_path, monkeypatch):
    inbox = PhoneInbox(tmp_path)
    first = inbox.save("photo.jpg", b64(b"one"))
    second = inbox.save("photo.jpg", b64(b"two"))
    assert first.read_bytes() == b"one" and second.name == "photo (2).jpg" and first.parent.parent.name == "phone_inbox"
    assert inbox.recent()[0] in (first, second)
    for bad in ("", "%%%", b64(b"")):
        with pytest.raises(InboxError):
            inbox.save("x", bad)
    import app.remote.phone_inbox as mod

    monkeypatch.setattr(mod, "MAX_FILE_BYTES", 2)
    with pytest.raises(InboxError) as err:
        inbox.save("big", b64(b"three"))
    assert err.value.code == "too_big"


def test_upload_end_to_end_signed(db, tmp_path):
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from app.remote.pairing import PairingStore
    from app.remote.server import RemoteHub, RemoteServer

    reg = mobile.MobileRegistry(db)
    phone = paired(reg)
    hub = RemoteHub(lambda t: None, lambda ok, rid: False, lambda: None, lambda: {})
    received, clip = [], []
    hub.mobile, hub.phone_inbox = reg, PhoneInbox(tmp_path)
    hub.on_file = lambda name, path: received.append(path)
    hub.clipboard = clip.append
    server = RemoteServer(hub, PairingStore(None, encrypt=False), 0)
    good = phone.envelope("phone_file", {"name": "../note.txt", "data": b64(b"hello")})
    wrong = phone.envelope("phone_clipboard", {"text": "x"})               # only files on /upload
    clipboard = phone.envelope("phone_clipboard", {"text": "copied on the phone"})

    async def go():
        client = TestClient(TestServer(server.app()))
        await client.start_server()
        try:
            a = await client.post("/api/mobile/upload", json=good)
            b = await client.post("/api/mobile/upload", json=wrong)
            c = await client.post("/api/mobile/command", json=clipboard)
            return a.status, await a.json(), b.status, await c.json()
        finally:
            await client.close()

    status, body, wrong_status, clip_answer = asyncio.run(go())
    assert status == 200 and body["ok"] and received and received[0].read_bytes() == b"hello"
    assert received[0].name == "note.txt" and wrong_status == 400
    assert clip_answer["ok"] and clip == ["copied on the phone"]


def test_new_phone_actions_and_their_risk():
    from app.tools.phone_link_tool import PhoneArgs, _confirmation, _risk

    assert check_action("tap_text", {"text": "  Send  "}) == {"text": "Send"}
    assert check_action("set_clipboard", {"text": "hi"}) == {"text": "hi"}
    assert check_action("read_screen", {}) == {} and check_action("locate", {}) == {}
    with pytest.raises(ValueError):
        check_action("tap_text", {"text": ""})
    assert _risk(PhoneArgs(action="read_screen"), None) == RiskLevel.MODERATE
    assert _risk(PhoneArgs(action="tap_text", text="Send"), None) == RiskLevel.MODERATE
    assert _risk(PhoneArgs(action="locate"), None) == RiskLevel.MODERATE
    assert _risk(PhoneArgs(action="set_clipboard", text="x"), None) == RiskLevel.SAFE
    assert "passwords are never read" in _confirmation(PhoneArgs(action="read_screen"), None)


def test_phone_side_never_reads_passwords_and_knows_the_actions():
    control = (ANDROID / "src/com/jarvis/mobile/ControlService.java").read_text(encoding="utf-8")
    actions = (ANDROID / "src/com/jarvis/mobile/PhoneActions.java").read_text(encoding="utf-8")
    assert "node.isPassword() ? null" in control and "!node.isPassword()" in control
    for action in ("read_screen", "tap_text", "set_clipboard", "locate"):
        assert f'"{action}"' in actions


@pytest.mark.skipif(shutil.which("javac") is None, reason="needs a JDK (javac)")
def test_all_android_sources_compile():
    done = subprocess.run(["sh", str(ANDROID / "test/compile_all.sh")], capture_output=True, text=True, shell=False)
    assert done.returncode == 0, done.stdout + done.stderr
