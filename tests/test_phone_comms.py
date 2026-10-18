"""JARVIS Mobile 1.2 from the PC: calls, SMS, reading messages, contacts, gallery / camera, asking the phone to
share its screen - with the safety rules: calls/SMS always ask, no USSD codes, a separate switch on the phone."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app.remote.phone_link import COMMS, PhoneLink, check_action
from app.security.permissions import RiskLevel
from tests.test_phone_link import _ctx, _phone, db  # noqa: F401  (fixture)

ANDROID = Path(__file__).resolve().parents[1] / "android" / "JarvisMobile"


def test_argument_rules():
    assert check_action("call", {"number": "Ammu"}) == {"number": "Ammu"}
    assert check_action("call", {"number": "+880 1712-345678"})["number"] == "+880 1712-345678"
    assert check_action("send_sms", {"number": "আম্মু", "text": " on my way "}) == {"number": "আম্মু", "text": "on my way"}
    assert check_action("read_messages", {"count": 99, "from": "Bank"}) == {"count": 10, "from": "Bank"}
    assert check_action("open_gallery", {}) == {} and check_action("share_screen", {}) == {}
    for action, args in (("call", {"number": "*21*01712345678#"}), ("call", {"number": ""}),
                         ("send_sms", {"number": "Ammu", "text": ""}), ("find_contact", {"name": "a;DROP"})):
        with pytest.raises(ValueError):
            check_action(action, args)


def test_risk_levels_and_questions():
    from app.tools.phone_link_tool import PhoneArgs, _confirmation, _risk

    assert _risk(PhoneArgs(action="call", number="Ammu"), None) == RiskLevel.DANGEROUS
    assert _risk(PhoneArgs(action="send_sms", number="Ammu", text="hi"), None) == RiskLevel.DANGEROUS
    assert _risk(PhoneArgs(action="read_messages"), None) == RiskLevel.MODERATE
    assert _risk(PhoneArgs(action="open_gallery"), None) == RiskLevel.SAFE
    assert "Ammu" in _confirmation(PhoneArgs(action="call", number="Ammu"), None)
    assert '"hi"' in _confirmation(PhoneArgs(action="send_sms", number="Ammu", text="hi"), None)


def test_comms_need_the_phones_own_switch(db, tmp_path, monkeypatch):  # noqa: F811
    from app.tools.base import ToolError
    from app.tools.phone_link_tool import PhoneArgs, phone_control

    _phone(db)
    link = PhoneLink(db)
    link.collect("phone-0001", {"control": True, "comms": False, "app_version": "1.2.0"})
    ctx = _ctx(db, tmp_path)
    with pytest.raises(ToolError) as err:
        asyncio.run(phone_control(PhoneArgs(action="call", number="Ammu"), ctx))
    assert err.value.code == "not_allowed" and "Allow calls and messages" in str(err.value)
    link.collect("phone-0001", {"control": True, "comms": True, "app_version": "1.2.0"})

    def answering_wait(self, command_id, seconds=8.0, sleep=None):
        self.collect("phone-0001", {})
        self.report("phone-0001", command_id, True, "Ammu (1 Jan 09:00): code •••• " + "x" * 600)
        return self.outcome(command_id)

    monkeypatch.setattr(PhoneLink, "wait", answering_wait)
    done = asyncio.run(phone_control(PhoneArgs(action="read_messages", count=3, sender="Ammu"), ctx))
    assert done.ok and done.message.startswith("Ammu") and len(done.message) > 300      # long results come through


def test_ask_the_phone_to_share(db, tmp_path):  # noqa: F811
    from types import SimpleNamespace

    from app.ui.controller_parts.remote import RemoteMixin

    host = RemoteMixin.__new__(RemoteMixin)
    host.ctx = SimpleNamespace(database=db)
    assert "No trusted phone" in host.ask_phone_to_share()
    _phone(db)
    PhoneLink(db).collect("phone-0001", {"control": True})
    assert "Tap 'Start now'" in host.ask_phone_to_share()
    assert [c["action"] for c in PhoneLink(db).collect("phone-0001", {})] == ["share_screen"]


def test_phone_side_knows_every_action_the_pc_sends():
    phone = (ANDROID / "src/com/jarvis/mobile/PhoneActions.java").read_text(encoding="utf-8")
    comms = (ANDROID / "src/com/jarvis/mobile/Comms.java").read_text(encoding="utf-8")
    from app.remote.phone_link import ACTIONS

    for action in ACTIONS:
        assert f'"{action}"' in phone or f'"{action}"' in comms, action
    assert all(f'"{a}"' in comms for a in COMMS)
    assert 'contains("*") || who.contains("#")' in comms                # never USSD codes


@pytest.mark.skipif(shutil.which("javac") is None, reason="needs a JDK (javac)")
def test_android_sources_compile():
    done = subprocess.run(["sh", str(ANDROID / "test/compile_all.sh")], capture_output=True, text=True, shell=False)
    assert done.returncode == 0, done.stdout + done.stderr
