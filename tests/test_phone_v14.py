"""JARVIS Mobile 1.4 (PC side): phone-event automations (charger, low battery) and locking the phone."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.automation.scheduler import Scheduler
from app.automation.triggers import EventTrigger
from app.memory.database import Database
from app.remote import phone_link
from app.remote.phone_link import PhoneLink, check_action
from app.security.permissions import RiskLevel
from tests.test_phone_link import _phone

ANDROID = Path(__file__).resolve().parents[1] / "android" / "JarvisMobile"


def due(event: str, value: str, before: dict, now: dict) -> bool:
    return Scheduler._event_due(EventTrigger(event=event, value=value), {"phone": before}, {"phone": now})


def test_charger_trigger_fires_on_change_only():
    plugged, unplugged = {"battery": 50, "charging": True}, {"battery": 50, "charging": False}
    assert due("phone_charging", "started", unplugged, plugged) and not due("phone_charging", "stopped", unplugged, plugged)
    assert due("phone_charging", "stopped", plugged, unplugged) and due("phone_charging", "", plugged, unplugged)
    assert not due("phone_charging", "", plugged, plugged)
    assert not due("phone_charging", "", {}, plugged)                 # a phone not seen before never "changes"
    assert not due("phone_charging", "", {"battery": 50}, plugged)    # unknown charging state


def test_low_battery_trigger_fires_once_when_crossing():
    assert due("phone_battery_low", "20", {"battery": 22, "charging": False}, {"battery": 19, "charging": False})
    assert not due("phone_battery_low", "20", {"battery": 19, "charging": False}, {"battery": 18, "charging": False})
    assert not due("phone_battery_low", "20", {"battery": 22, "charging": True}, {"battery": 19, "charging": True})
    assert due("phone_battery_low", "", {"battery": 20, "charging": False}, {"battery": 19, "charging": False})   # 20 default
    assert "phone" in EventTrigger(event="phone_charging", value="started").describe()


def test_collect_publishes_what_automations_look_at(tmp_path: Path):
    db = Database(tmp_path / "x.db")
    db.initialize()
    _phone(db)
    phone_link.LATEST.clear()
    PhoneLink(db).collect("phone-0001", {"battery": 61, "charging": True, "lock": True})
    assert phone_link.LATEST == {"battery": 61, "charging": True}
    assert PhoneLink(db).state("phone-0001")["lock"] is True


def test_lock_phone_is_known_and_always_asks():
    from app.tools.phone_link_tool import PhoneArgs, _confirmation, _risk

    assert check_action("lock_phone", {}) == {}
    args = PhoneArgs(action="lock_phone")
    assert _risk(args, None) == RiskLevel.DANGEROUS and "Lock" in _confirmation(args, None)
    with pytest.raises(ValueError):
        check_action("wipe_phone", {})


def test_lock_admin_has_only_the_lock_power():
    policy = (ANDROID / "res" / "xml" / "device_admin.xml").read_text(encoding="utf-8")
    import re

    tags = re.findall(r"<([a-z-]+)\s*/>", re.sub(r"<!--.*?-->", "", policy, flags=re.S))
    assert tags == ["force-lock"]                       # the only policy: no wipe, password rules or camera control
    manifest = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
    assert ".LockAdmin" in manifest and "BIND_DEVICE_ADMIN" in manifest and 'versionName="1.5.0"' in manifest
