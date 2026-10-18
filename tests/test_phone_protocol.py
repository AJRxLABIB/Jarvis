"""JARVIS Mobile protocol hardening: version field, idempotent request ids (a retried request never runs twice),
cancelling phone requests the phone hasn't taken, 'unsupported' answers from older apps, schema v36 keeps rows."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app.memory.database import LATEST_SCHEMA_VERSION, Database
from app.remote import mobile
from app.remote.phone_link import PhoneLink
from tests.test_phone_link import _phone
from tests.test_v3150 import paired  # a real paired phone with real keys


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "x.db")
    database.initialize()
    return database


def test_protocol_version(db):
    reg = mobile.MobileRegistry(db)
    phone = paired(reg)
    reg.verify(phone.envelope("status"))                             # no field = version 1 (older apps)
    env = phone.envelope("status")
    env["protocol"] = 1
    reg.verify(env)
    env = phone.envelope("status")
    env["protocol"] = 99
    with pytest.raises(mobile.MobileError) as err:
        reg.verify(env)
    assert err.value.code == "protocol_unsupported" and err.value.status == 426


def test_retried_request_id_returns_the_first_answer(db):
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from app.remote.pairing import PairingStore
    from app.remote.server import RemoteHub, RemoteServer

    reg = mobile.MobileRegistry(db)
    phone = paired(reg)
    sent = []
    hub = RemoteHub(sent.append, lambda ok, rid: False, lambda: None, lambda: {})
    hub.mobile = reg
    server = RemoteServer(hub, PairingStore(None, encrypt=False), 0)
    first = phone.envelope("chat", {"text": "hello"})
    retry = phone.envelope("chat", {"text": "hello"})               # new nonce + signature, same request id
    retry["request_id"] = first["request_id"]
    retry["signature"] = base64.b64encode(phone.key.sign(mobile.signed_text(retry), ec.ECDSA(hashes.SHA256()))).decode()

    async def go():
        client = TestClient(TestServer(server.app()))
        await client.start_server()
        try:
            a = await (await client.post("/api/mobile/command", json=first)).json()
            b = await (await client.post("/api/mobile/command", json=retry)).json()
            c = await (await client.post("/api/mobile/command", json=first)).json()     # exact replay: refused
            return a, b, c
        finally:
            await client.close()

    a, b, c = asyncio.run(go())
    assert a["ok"] and b.get("duplicate") is True and sent == ["hello"]          # ran once
    assert c["ok"] is False and c["error"] == "replay"


def test_cancel_pending_phone_requests(db):
    _phone(db)
    link = PhoneLink(db)
    first, second = link.queue("phone-0001", "ring"), link.queue("phone-0001", "open_gallery")
    assert link.cancel("phone-0001", first) == 1
    assert [c["id"] for c in link.collect("phone-0001", {})] == [second]       # the cancelled one never reaches it
    assert link.cancel("phone-0001") == 0                                       # already taken: can't call it back
    assert link.outcome(first)["status"] == "cancelled"


def test_unsupported_answer_from_an_older_app(db):
    _phone(db)
    link = PhoneLink(db)
    cid = link.queue("phone-0001", "open_gallery")
    link.collect("phone-0001", {})
    link.report("phone-0001", cid, False, "this phone app doesn't know 'open_gallery' (update JARVIS Mobile)")
    assert link.outcome(cid)["status"] == "unsupported"


def test_phone_control_cancel_pending_and_shortcut(db, tmp_path):
    from app.agent.shortcuts import match_shortcut
    from app.tools.phone_link_tool import PhoneArgs, phone_control
    from tests.test_phone_link import _ctx

    _phone(db)
    link = PhoneLink(db)
    link.queue("phone-0001", "ring")
    result = asyncio.run(phone_control(PhoneArgs(action="cancel_pending"), _ctx(db, tmp_path)))
    assert result.ok and result.data["cancelled"] == 1
    call = match_shortcut("cancel the phone request", lambda n: None)
    assert call.name == "phone_control" and call.arguments == {"action": "cancel_pending"}


def test_migration_36_keeps_phone_rows(tmp_path):
    path = tmp_path / "old.db"
    database = Database(path)
    database.initialize()
    assert database.schema_version() == LATEST_SCHEMA_VERSION >= 36
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO phone_commands (device_id, action, args, created, expires, status) "
                     "VALUES ('p', 'ring', '{}', 'a', 'b', 'cancelled')")
        assert conn.execute("SELECT status FROM phone_commands").fetchone()[0] == "cancelled"
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'phone_commands'").fetchone()[0]
    assert "unsupported" in sql and json.dumps(sql)
