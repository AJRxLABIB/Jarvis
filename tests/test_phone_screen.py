"""Seeing / controlling the phone's screen from the PC: the frame hub, input rules, the viewer, and the Android side
compiling (against compile-only stand-ins) when a JDK is available."""

import base64
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.remote import mobile
from app.remote.phone_screen import MAX_FRAME_BYTES, PhoneScreenHub, ScreenError, make_input

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
PHONE = SimpleNamespace(id="p1", name="Pixel")
ANDROID = Path(__file__).resolve().parents[1] / "android" / "JarvisMobile"


def frame(hub, control=False, data=JPEG, **extra):
    return hub.frame(PHONE, {"jpeg": base64.b64encode(data).decode(), "width": 540, "height": 1200,
                             "control": control, **extra})


def test_commands_are_read_only_and_not_step_up():
    for command in ("screen_frame", "screen_stopped"):
        assert mobile.COMMANDS[command] == ("read", None) and command not in mobile.STEP_UP_COMMANDS
    assert mobile.FRAME_ARGUMENT_CHARS > MAX_FRAME_BYTES * 4 // 3


def test_frames_are_kept_in_memory_only_and_bad_frames_refused():
    hub = PhoneScreenHub()
    assert frame(hub) == {"ok": True, "stop": False, "input": []}
    share = hub.latest("p1")
    assert share.frame == JPEG and share.width == 540 and [s.device_id for s in hub.active()] == ["p1"]
    for bad in ({"jpeg": "%%%"}, {"jpeg": base64.b64encode(b"GIF89a").decode()}, {}):
        with pytest.raises(ScreenError):
            hub.frame(PHONE, bad)
    with pytest.raises(ScreenError):
        frame(hub, data=b"\xff\xd8" + b"0" * (MAX_FRAME_BYTES + 1))


def test_input_only_when_the_phone_allows_control():
    hub = PhoneScreenHub()
    frame(hub, control=False)
    assert not hub.send_input("p1", "tap", x=0.5, y=0.5)            # view only
    frame(hub, control=True)
    assert hub.send_input("p1", "tap", x=2.0, y=-1, long=True)
    assert hub.send_input("p1", "back")
    answer = frame(hub, control=True)
    assert answer["input"] == [{"kind": "tap", "x": 1.0, "y": 0.0, "long": True}, {"kind": "back"}]
    assert frame(hub, control=True)["input"] == []                  # delivered once


def test_input_validation():
    assert make_input("swipe", x=0, y=0, x2=1, y2=1, ms=99999)["ms"] == 2000
    for kind, values in (("format_disk", {}), ("tap", {"x": "a", "y": 1}), ("tap", {"x": float("nan"), "y": 0}),
                         ("text", {"text": ""})):
        with pytest.raises(ScreenError):
            make_input(kind, **values)


def test_the_pc_can_stop_sharing_and_emergency_stop_reaches_it():
    hub = PhoneScreenHub()
    frame(hub, control=True)
    hub.send_input("p1", "home")
    hub.stop("p1")
    assert hub.active() == []
    assert frame(hub, control=True) == {"ok": True, "stop": True, "input": []}   # the phone is told, input dropped
    frame(hub)
    hub.stop_all()
    assert hub.active() == [] and hub.latest("p1").frame == b""
    frame(hub)
    hub.stopped(PHONE)
    assert hub.latest("p1") is None


def test_stale_share_disappears():
    now = [0.0]
    hub = PhoneScreenHub(clock=lambda: now[0])
    frame(hub)
    now[0] = 30.0
    assert hub.active() == []


def test_gesture_classification():
    from app.ui.phone_screen_view import classify

    assert classify((0.5, 0.5), (0.505, 0.5), 0.1) == {"kind": "tap", "x": 0.5, "y": 0.5, "long": False}
    assert classify((0.5, 0.5), (0.5, 0.5), 1.0)["long"] is True
    swipe = classify((0.5, 0.8), (0.5, 0.2), 0.3)
    assert swipe["kind"] == "swipe" and swipe["ms"] == 300


def test_viewer_window_shows_the_shared_phone():
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication

    from app.ui.phone_screen_view import PhoneScreenWindow

    QApplication.instance() or QApplication([])
    image = QImage(54, 120, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "JPEG")
    hub = PhoneScreenHub()
    window = PhoneScreenWindow(hub)
    assert "Waiting" in window.status.text()
    frame(hub, control=False, data=bytes(data))
    window.refresh()
    assert window.devices.count() == 1 and window.canvas.image is not None and "View only" in window.status.text()
    window.canvas.gesture.emit({"kind": "tap", "x": 0.2, "y": 0.3, "long": False})
    assert "hasn't allowed" in window.status.text()
    window.stop_sharing()
    assert hub.active() == []
    window.close()


@pytest.mark.skipif(shutil.which("javac") is None, reason="needs a JDK (javac)")
def test_every_android_source_compiles_against_the_stand_ins():
    done = subprocess.run(["sh", str(ANDROID / "test/compile_all.sh")], capture_output=True, text=True, shell=False)
    assert done.returncode == 0 and "ALL SOURCES COMPILE" in done.stdout, done.stdout + done.stderr


def test_manifest_declares_the_services_safely():
    manifest = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
    assert 'android:name=".ScreenShareService" android:exported="false"' in manifest
    assert 'android:foregroundServiceType="mediaProjection"' in manifest
    assert 'android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE"' in manifest
    assert (ANDROID / "res/xml/jarvis_control.xml").is_file() and (ANDROID / "res/values/strings.xml").is_file()


def test_frame_endpoint_end_to_end_signed(tmp_path):
    """A real signed frame through /api/mobile/frame; an unsigned / tampered one is refused."""
    pytest.importorskip("aiohttp")
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from app.memory.database import Database
    from app.remote.mobile import MobileRegistry
    from app.remote.pairing import PairingStore
    from app.remote.server import RemoteHub, RemoteServer
    from tests.test_v3150 import paired          # a real paired phone with real P-256 keys

    database = Database(tmp_path / "j.db")
    database.initialize()
    reg = MobileRegistry(database)
    phone = paired(reg)
    hub = RemoteHub(lambda t: None, lambda ok, rid: False, lambda: None, lambda: {})
    hub.mobile, hub.phone_screen = reg, PhoneScreenHub()
    server = RemoteServer(hub, PairingStore(None, encrypt=False), 0)
    good = phone.envelope("screen_frame", {"jpeg": base64.b64encode(JPEG).decode(), "width": 10, "height": 20,
                                           "control": True})
    tampered = dict(phone.envelope("screen_frame", {"jpeg": base64.b64encode(JPEG).decode()}))
    tampered["arguments"] = tampered["arguments"].replace("/", "+")
    wrong = phone.envelope("status", {})

    async def go():
        client = TestClient(TestServer(server.app()))
        await client.start_server()
        try:
            ok = await client.post("/api/mobile/frame", json=good)
            bad = await client.post("/api/mobile/frame", json=tampered)
            other = await client.post("/api/mobile/frame", json=wrong)
            return ok.status, await ok.json(), bad.status, other.status
        finally:
            await client.close()

    status, body, bad, other = asyncio.run(go())
    assert status == 200 and body["ok"] and body["stop"] is False
    assert hub.phone_screen.latest(phone.device_id).frame == JPEG
    assert bad in (400, 401, 403) and other == 400
