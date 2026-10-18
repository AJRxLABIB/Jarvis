"""PC -> phone actions (queue / collect / report / expiry), phone -> PC notices, the phone_control tool."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.memory.database import Database
from app.remote import phone_link
from app.remote.phone_link import PhoneLink, check_action, redact_codes


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "x.db")
    database.initialize()
    return database


def _phone(db: Database, device: str = "phone-0001", trust: str = "trusted", name: str = "Pixel") -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO mobile_devices (id, name, public_key, fingerprint, trust, paired_at) "
                     "VALUES (?, ?, 'k', ?, ?, 'x')", (device, name, "fp" + device, trust))


def test_redaction_and_argument_checks() -> None:
    assert redact_codes("Your code is 482913. Call 01712345678") == "Your code is ••••. Call 01712345678"
    assert redact_codes("PIN 1234 ok") == "PIN •••• ok" and redact_codes("") == ""
    assert check_action("open_link", {"url": "https://example.com/x"}) == {"url": "https://example.com/x"}
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "intent://x", ""):
        with pytest.raises(ValueError):
            check_action("open_link", {"url": bad})
    with pytest.raises(ValueError):
        check_action("format_phone", {})
    with pytest.raises(ValueError):
        check_action("open_app", {"app": "  "})
    assert check_action("ring", {"seconds": 999}) == {"seconds": 60} and check_action("ring", {}) == {"seconds": 15}
    assert len(check_action("show_text", {"text": "x" * 900})["text"]) == 500


def test_queue_collect_report_once_and_only_by_its_own_phone(db: Database) -> None:
    link = PhoneLink(db)
    cid = link.queue("phone-0001", "flashlight_on")
    assert link.outcome(cid)["status"] == "pending"
    assert link.collect("phone-0002") == []                                 # another phone gets nothing
    got = link.collect("phone-0001", {"battery": 64, "charging": True, "control": True, "secret": "x"})
    assert got == [{"id": cid, "action": "flashlight_on", "args": {}}]
    assert link.collect("phone-0001") == []                                 # handed over once
    assert link.state("phone-0001")["battery"] == 64 and "secret" not in link.state("phone-0001")
    assert not link.report("phone-0002", cid, True, "spoofed")             # not that phone's command
    assert link.report("phone-0001", cid, True, "Flashlight on")
    assert not link.report("phone-0001", cid, False, "replay")             # no second answer
    assert link.outcome(cid) == {"status": "done", "result": "Flashlight on"}


def test_old_actions_expire_instead_of_running_later(db: Database, monkeypatch) -> None:
    link = PhoneLink(db)
    cid = link.queue("phone-0001", "ring")
    later = datetime.now(timezone.utc) + timedelta(seconds=phone_link.EXPIRE_SECONDS + 5)
    monkeypatch.setattr(phone_link, "_now", lambda: later)
    assert link.outcome(cid)["status"] == "expired"
    assert link.collect("phone-0001") == [] and link.outcome(cid)["status"] == "expired"
    assert link.wait(cid, seconds=0.1, sleep=lambda _s: None)["status"] == "expired"


def _ctx(db: Database, tmp_path: Path):
    from app.config.settings import Settings
    from app.tools.base import ToolContext

    return ToolContext(settings=Settings(data_dir=tmp_path), database=db)


def test_phone_control_tool(db: Database, tmp_path: Path, monkeypatch) -> None:
    from app.tools.base import ToolError
    from app.tools.phone_link_tool import PhoneArgs, phone_control

    ctx = _ctx(db, tmp_path)
    with pytest.raises(ToolError) as err:
        asyncio.run(phone_control(PhoneArgs(action="status"), ctx))
    assert err.value.code == "no_phone"
    _phone(db, "phone-9", trust="revoked", name="Old")
    _phone(db)
    assert "hasn't checked in" in asyncio.run(phone_control(PhoneArgs(action="status"), ctx)).message
    link = PhoneLink(db)
    link.collect("phone-0001", {"battery": 40, "control": False})
    status = asyncio.run(phone_control(PhoneArgs(action="status"), ctx))
    assert "battery 40%" in status.message and "OFF" in status.message
    with pytest.raises(ToolError) as err:                                   # the phone said no
        asyncio.run(phone_control(PhoneArgs(action="ring"), ctx))
    assert err.value.code == "not_allowed"
    link.collect("phone-0001", {"control": True})
    with pytest.raises(ToolError):
        asyncio.run(phone_control(PhoneArgs(action="open_link", url="ftp://x"), ctx))

    def answering_wait(self, command_id, seconds=8.0, sleep=None):          # the phone answers at once
        self.collect("phone-0001", {"control": True})
        self.report("phone-0001", command_id, True, "Opened WhatsApp")
        return self.outcome(command_id)

    monkeypatch.setattr(PhoneLink, "wait", answering_wait)
    done = asyncio.run(phone_control(PhoneArgs(action="open_app", app="WhatsApp"), ctx))
    assert done.ok and done.message == "Opened WhatsApp"
    monkeypatch.setattr(PhoneLink, "wait", lambda self, cid, seconds=8.0, sleep=None: self.outcome(cid))
    pending = asyncio.run(phone_control(PhoneArgs(action="flashlight_on"), ctx))
    assert pending.ok and "couldn't confirm" in pending.message and pending.data.get("verified") is False


def test_phone_control_is_known_to_the_router_and_autonomy() -> None:
    from app.security import autonomy
    from app.tools.catalog import build_default_registry

    assert build_default_registry().get("phone_control") is not None
    assert autonomy.blocked_in("observe", "phone_control", {"action": "status"}) is None
    assert autonomy.blocked_in("observe", "phone_control", {"action": "ring"}) is not None


def test_server_phone_commands(db: Database) -> None:
    from app.core.notifications import NotificationCenter
    from app.remote.mobile import COMMANDS, MobileError
    from app.remote.server import RemoteServer

    assert COMMANDS["phone_commands"][0] == "read" and COMMANDS["phone_share"][0] == "sync"
    server = SimpleNamespace(hub=SimpleNamespace(mobile=SimpleNamespace(database=db)))
    device = SimpleNamespace(id="phone-0001", name="Pixel", trust="trusted", permissions=[])

    def run(command, arguments):
        return asyncio.run(RemoteServer._mobile_run(server, device, command, arguments))

    cid = PhoneLink(db).queue("phone-0001", "show_text", {"text": "hi"})
    assert run("phone_commands", {"report": {"battery": 80}})["commands"][0]["id"] == cid
    with pytest.raises(MobileError):
        run("phone_result", {"id": "1"})
    assert run("phone_result", {"id": cid, "ok": True, "message": "Shown"})["ok"]
    run("phone_notify", {"app": "Bank", "title": "OTP", "text": "Your code is 552211"})
    assert run("phone_share", {"text": "https://example.com"})["ok"]
    with pytest.raises(MobileError):
        run("phone_share", {"text": "  "})
    items = NotificationCenter(db).items(category="phone")
    texts = " ".join(f"{i.title} {i.body}" if hasattr(i, "title") else str(i) for i in items)
    assert "552211" not in texts and "••••" in texts and "Shared from your phone" in texts
