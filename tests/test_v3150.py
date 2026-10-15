"""3.15.0: JARVIS Mobile secure device protocol - pairing, trust, signed commands, replay protection,
permissions, revocation, and commands through the executor. Adversarial cases included."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.memory.database import Database
from app.remote import mobile
from app.remote.mobile import MobileError, MobileRegistry


class Phone:
    """What the Android app does (Keystore P-256 key, pairing proof, signed envelopes)."""

    def __init__(self, name: str = "Pixel 8") -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        self.auth_key = ec.generate_private_key(ec.SECP256R1())  # the biometric-gated key (JARVIS Mobile 1.0)
        self.auth_public = base64.b64encode(self.auth_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        self.name, self.device_id = name, ""

    def sign(self, data: bytes) -> str:
        return base64.b64encode(self.key.sign(data, ec.ECDSA(hashes.SHA256()))).decode()

    def register_auth(self, registry) -> None:
        proof = base64.b64encode(self.auth_key.sign(
            ("auth-key\n" + self.device_id + "\n" + self.auth_public).encode(),
            ec.ECDSA(hashes.SHA256()))).decode()
        device = registry.get(self.device_id)
        registry.register_auth_key(device, self.auth_public, proof)

    def pair_request(self, session, secret: str | None = None) -> dict:
        text = mobile.pairing_text(session.id, self.public, self.name)
        proof = base64.b64encode(hmac.new((secret or session.secret).encode(), text, hashlib.sha256).digest()).decode()
        return {"pairing_id": session.id, "public_key": self.public, "name": self.name, "platform": "android",
                "os_version": "15", "app_version": "1.1", "key_info": {"hardware": True, "user_auth": True},
                "proof": proof, "signature": self.sign(text)}

    def envelope(self, command: str, arguments: dict | None = None, *, timestamp: int | None = None,
                 nonce: str | None = None, key=None) -> dict:
        envelope = {"request_id": secrets.token_hex(8), "device_id": self.device_id,
                    "timestamp": timestamp if timestamp is not None else int(time.time() * 1000),
                    "nonce": nonce or secrets.token_urlsafe(18), "command": command,
                    "arguments": json.dumps(arguments or {})}
        signer = key or (self.auth_key if command in mobile.STEP_UP_COMMANDS else self.key)
        envelope["signature"] = base64.b64encode(signer.sign(mobile.signed_text(envelope),
                                                             ec.ECDSA(hashes.SHA256()))).decode()
        return envelope


@pytest.fixture()
def registry(tmp_path: Path) -> MobileRegistry:
    database = Database(tmp_path / "j.db")
    database.initialize()
    return MobileRegistry(database)


def paired(registry: MobileRegistry, trust: str = "trusted", *, auth_key: bool = True) -> Phone:
    phone = Phone()
    device = registry.pair(phone.pair_request(registry.start_pairing()))
    phone.device_id = device.id
    if auth_key:
        phone.register_auth(registry)
    if trust != "pending":
        registry.set_trust(device.id, trust)
    return phone


def code_of(call) -> str:
    with pytest.raises(MobileError) as caught:
        call()
    return caught.value.code


# ------------------------------------------------------------------ pairing


def test_pairing_makes_a_pending_phone_that_can_do_nothing_yet(registry) -> None:
    phone = paired(registry, trust="pending")
    device = registry.get(phone.device_id)
    assert device.trust == "pending" and device.fingerprint == mobile.fingerprint(phone.public)
    assert device.permissions == list(mobile.DEFAULT_PERMISSIONS)
    assert registry.verify(phone.envelope("hello"))[1] == "hello"  # it may ask whether it's trusted
    assert code_of(lambda: registry.verify(phone.envelope("status"))) == "pending"


def test_pairing_session_is_one_use_and_expires(registry) -> None:
    session = registry.start_pairing()
    registry.pair(Phone().pair_request(session))
    assert code_of(lambda: registry.pair(Phone("Second").pair_request(session))) == "pairing_expired"
    clock = [time.time()]
    registry._clock = lambda: clock[0]
    late = registry.start_pairing()
    clock[0] += mobile.PAIRING_SECONDS + 1
    assert code_of(lambda: registry.pair(Phone().pair_request(late))) == "pairing_expired"
    assert registry.pairing_left() == 0


def test_malicious_qr_or_wrong_secret_is_refused_and_recorded(registry) -> None:
    session = registry.start_pairing()
    assert code_of(lambda: registry.pair(Phone().pair_request(session, secret="guessed"))) == "bad_proof"
    kinds = [e["kind"] for e in registry.security_events()]
    assert "pairing_refused" in kinds and not registry.all()


def test_phone_must_prove_it_holds_the_key(registry) -> None:
    session = registry.start_pairing()
    request = Phone().pair_request(session)
    request["signature"] = Phone().sign(b"something else")  # a stolen public key without its private key
    assert code_of(lambda: registry.pair(request)) == "bad_signature"


def test_only_p256_keys(registry) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = base64.b64encode(rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
                               .public_bytes(serialization.Encoding.DER,
                                             serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    assert code_of(lambda: mobile.load_public_key(rsa_key)) == "bad_key"
    assert code_of(lambda: mobile.load_public_key("not base64!")) == "bad_key"


# ------------------------------------------------------------------ signed commands


def test_valid_signed_command(registry) -> None:
    phone = paired(registry)
    device, command, arguments = registry.verify(phone.envelope("chat", {"text": "hello"}))
    assert command == "chat" and arguments == {"text": "hello"} and device.last_seen


def test_invalid_signature_fake_device_and_mitm_changes(registry) -> None:
    phone = paired(registry)
    other = Phone()
    assert code_of(lambda: registry.verify(phone.envelope("status", key=other.key))) == "bad_signature"
    tampered = phone.envelope("chat", {"text": "hello"})
    tampered["arguments"] = json.dumps({"text": "delete everything"})  # changed on the way
    assert code_of(lambda: registry.verify(tampered)) == "bad_signature"
    fake = Phone()
    fake.device_id = "deadbeefdeadbeef"
    assert code_of(lambda: registry.verify(fake.envelope("status"))) == "unknown_device"


def test_replayed_and_expired_requests(registry) -> None:
    phone = paired(registry)
    envelope = phone.envelope("status")
    registry.verify(envelope)
    assert code_of(lambda: registry.verify(envelope)) == "replay"
    old = int(time.time() * 1000) - mobile.CLOCK_WINDOW_MS - 1000
    assert code_of(lambda: registry.verify(phone.envelope("status", timestamp=old))) == "expired"
    future = int(time.time() * 1000) + mobile.CLOCK_WINDOW_MS + 1000
    assert code_of(lambda: registry.verify(phone.envelope("status", timestamp=future))) == "expired"
    assert "replay" in [e["kind"] for e in registry.security_events()]


@pytest.mark.parametrize("broken", [
    None, [], {"command": "status"}, "text",
    {"request_id": "x", "device_id": "y", "nonce": "z", "command": "status", "arguments": "{}", "signature": "s",
     "timestamp": 1},
])
def test_malformed_commands(registry, broken) -> None:
    assert code_of(lambda: registry.verify(broken)) == "malformed"


def test_unknown_command_and_bad_arguments(registry) -> None:
    phone = paired(registry)
    assert code_of(lambda: registry.verify(phone.envelope("run_shell", {"cmd": "format c:"}))) == "unknown_command"
    envelope = phone.envelope("chat")
    envelope["arguments"] = "[1, 2]"
    envelope["signature"] = phone.sign(mobile.signed_text(envelope))
    assert code_of(lambda: registry.verify(envelope)) == "malformed"


def test_permissions_are_enforced_and_default_to_minimum(registry) -> None:
    phone = paired(registry)
    assert code_of(lambda: registry.verify(phone.envelope("shutdown"))) == "not_permitted"
    assert code_of(lambda: registry.verify(phone.envelope("volume", {"action": "up"}))) == "not_permitted"
    registry.set_permissions(phone.device_id, ["read", "power"])
    assert registry.verify(phone.envelope("shutdown"))[1] == "shutdown"
    assert code_of(lambda: registry.verify(phone.envelope("chat", {"text": "x"}))) == "not_permitted"
    registry.set_permissions(phone.device_id, ["read", "files", "made_up"])
    assert "made_up" not in registry.get(phone.device_id).permissions


def test_restricted_phone_is_read_only_but_can_stop_everything(registry) -> None:
    phone = paired(registry)
    registry.set_permissions(phone.device_id, ["read", "chat", "power"])
    registry.set_trust(phone.device_id, "restricted")
    assert registry.get(phone.device_id).permissions == ["read"]
    registry.set_permissions(phone.device_id, ["power"])  # can't be widened while restricted
    assert "power" not in registry.get(phone.device_id).permissions
    assert registry.verify(phone.envelope("emergency_stop"))[1] == "emergency_stop"


def test_revoked_phone_loses_access_at_once(registry) -> None:
    phone = paired(registry)
    registry.verify(phone.envelope("status"))
    registry.set_trust(phone.device_id, "revoked")
    assert code_of(lambda: registry.verify(phone.envelope("status"))) == "revoked"
    assert code_of(lambda: registry.verify(phone.envelope("emergency_stop"))) == "revoked"
    with pytest.raises(ValueError):
        registry.set_trust(phone.device_id, "trusted")  # must pair again
    session = registry.start_pairing()
    again = Phone()
    again.key, again.public = phone.key, phone.public  # the same (lost) phone's key
    assert code_of(lambda: registry.pair(again.pair_request(session))) == "already_paired"


def test_commands_become_fixed_tool_calls_only() -> None:
    assert mobile.tool_call_for("shutdown", {"delay": 0, "force": True}) == ("power_control", {"action": "shutdown"})
    assert mobile.tool_call_for("volume", {"action": "set", "level": 40}) == ("volume_control",
                                                                              {"action": "set", "level": 40})
    for command, arguments in (("volume", {"action": "set", "level": 400}), ("volume", {"action": "rm -rf"}),
                               ("media", {"action": "format"}), ("open_app", {}), ("wake_device", {})):
        with pytest.raises(MobileError):
            mobile.tool_call_for(command, arguments)


def test_no_passwords_or_private_keys_are_stored(registry) -> None:
    phone = paired(registry)
    with registry._db.connect() as conn:
        dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM mobile_devices").fetchall()])
    private = phone.key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
    assert base64.b64encode(private).decode() not in dump and "password" not in dump.lower()


# ------------------------------------------------------------------ over HTTP, through the executor


def test_http_api_runs_commands_through_the_executor(registry) -> None:
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from app.remote.pairing import PairingStore
    from app.remote.server import RemoteHub, RemoteServer

    sent, answers, tools = [], [], []
    hub = RemoteHub(sent.append, lambda ok, rid: answers.append((ok, rid)) or True,
                    lambda: {"id": "q1", "text": "Empty the Recycle Bin?", "level": 2}, lambda: {"state": "IDLE"})
    hub.mobile = registry
    hub.emergency = lambda: "Stopping everything."

    async def run_tool(tool, arguments):
        tools.append((tool, arguments))
        return {"ok": True, "message": "Locked.", "error": None}

    hub.run_tool = run_tool
    server = RemoteServer(hub, PairingStore(None, encrypt=False), 0)
    phone = Phone()
    session = registry.start_pairing()

    async def go():
        client = TestClient(TestServer(server.app()))
        await client.start_server()
        try:
            paired_reply = await (await client.post("/api/mobile/pair", json=phone.pair_request(session))).json()
            phone.device_id = paired_reply["device_id"]
            phone.register_auth(registry)  # enrol the biometric-gated key (needed for lock)
            waiting = await client.post("/api/mobile/command", json=phone.envelope("status"))
            registry.set_trust(phone.device_id, "trusted")
            registry.set_permissions(phone.device_id, ["read", "chat", "approve", "power"])
            status = await (await client.post("/api/mobile/command", json=phone.envelope("status"))).json()
            await client.post("/api/mobile/command", json=phone.envelope("chat", {"text": "what time is it"}))
            approve = await (await client.post("/api/mobile/command", json=phone.envelope(
                "approve", {"id": "q1", "approved": True}))).json()
            lock = await (await client.post("/api/mobile/command", json=phone.envelope("lock"))).json()
            stop = await (await client.post("/api/mobile/command", json=phone.envelope("emergency_stop"))).json()
            replay_envelope = phone.envelope("status")
            await client.post("/api/mobile/command", json=replay_envelope)
            replay = await client.post("/api/mobile/command", json=replay_envelope)
            page = await client.get(f"/mobile/pair?pid={session.id}&s=abc")
            return (paired_reply, waiting.status, status, approve, lock, stop, replay.status, await page.text())
        finally:
            await client.close()

    paired_reply, waiting, status, approve, lock, stop, replay, page = asyncio.run(go())
    assert paired_reply["trust"] == "pending" and paired_reply["fingerprint"] == mobile.fingerprint(phone.public)
    assert waiting == 403 and status["status"] == {"state": "IDLE"}
    assert sent == ["what time is it"] and approve["ok"] and answers == [(True, "q1")]
    assert lock["ok"] and tools == [("power_control", {"action": "lock"})]
    assert stop["message"] == "Stopping everything." and replay == 409
    assert "jarviswake://pair?" in page and "pid=" in page


def test_mobile_devices_window_shows_trust_and_disables_unbuilt_permissions(registry) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from app.ui.mobile_devices import MobileDevicesDialog

    QApplication.instance() or QApplication([])
    paired(registry)
    dialog = MobileDevicesDialog(registry, lambda: None)
    dialog.list.setCurrentRow(0)
    assert "[trusted]" in dialog.list.item(0).text() and "Fingerprint" in dialog.details.text()
    assert not dialog.checks["files"].isEnabled() and not dialog.checks["admin"].isEnabled()
    assert dialog.checks["chat"].isChecked() and not dialog.checks["power"].isChecked()
    dialog.close()
