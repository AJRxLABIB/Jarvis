from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.shortcuts import match_shortcut
from app.config.settings import Settings
from app.core import security_center as sc
from app.memory.audit import AuditLog
from app.memory.database import Database
from app.memory.settings_store import SettingsStore


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "j.db")
    d.initialize()
    return d


def text(db, settings=None) -> str:
    return sc.describe(sc.collect(db, settings or Settings()))


def test_a_fresh_install_reports_facts_not_alarms(db) -> None:
    out = text(db)
    assert "Activity log intact" in out or "Activity log" in out.title() or "ACTIVITY LOG" in out
    assert "phone remote: off" in out and "no phone is paired" in out and "nothing was blocked" in out
    assert "no backup has been restore-tested yet" in out


def test_turning_a_guard_off_is_flagged(db) -> None:
    out = text(db, Settings(confirm_power_actions=False, update_require_signature=False,
                            update_manifest_url="https://example.com/jarvis.json"))
    assert "confirm power actions: OFF" in out and "update require signature: OFF" in out
    assert out.count("CHECK") >= 2 and "item(s) need a look" in out


def test_unencrypted_remote_is_flagged(db) -> None:
    out = text(db, Settings(remote_enabled=True, remote_https=False))
    assert "NOT encrypted" in out


def test_blocked_actions_appear_with_their_reason(db) -> None:
    audit = AuditLog(db)
    audit.record("tool_result", tool="power_control", level=3, decision="block",
                 details={"message": "Critical actions are never run automatically"})
    out = text(db)
    assert "power_control was blocked" in out and "never run automatically" in out and "nothing was blocked" not in out


def test_a_failed_restore_test_is_flagged_and_a_passed_one_is_not(db) -> None:
    kv = SettingsStore(db)
    kv.set("backup_restore_test", {"at": "2026-10-15T10:00:00", "ok": False, "message": "FAILED: boom"})
    assert "FAILED" in text(db) and "need a look" in text(db)
    kv.set("backup_restore_test", {"at": "2026-10-15T10:00:00", "ok": True, "message": "Restore test passed"})
    assert "passed" in text(db) and "Nothing needs attention" in text(db)


def test_a_tampered_activity_log_is_flagged(db) -> None:
    audit = AuditLog(db)
    for i in range(3):
        audit.record("tool_result", tool=f"t{i}", level=0, decision="allow", details={})
    with db.transaction() as conn:
        conn.execute("UPDATE audit_log SET tool = 'evil' WHERE id = 2")
    assert "FAILED verification" in text(db)


def test_voice_phrase() -> None:
    call = match_shortcut("open security center", lambda n: None)
    assert call is not None and call.arguments == {"action": "security_center"}


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


def test_dialog_shows_the_report_and_wires_only_available_buttons(db) -> None:
    from app.core import a11y
    from app.ui.security_center import SecurityCenterDialog

    QApplication.instance() or QApplication([])
    opened: list[str] = []
    dialog = SecurityCenterDialog(db, Settings(), open_phones=lambda: opened.append("phones"))
    assert "GUARDS ON THIS PC" in dialog.report.toPlainText()
    assert dialog.buttons["phones"].isEnabled() and not dialog.buttons["privacy"].isEnabled()
    dialog.buttons["phones"].click()
    assert opened == ["phones"]
    dialog.show()
    assert [f for f in a11y.audit_widgets(dialog) if f.level == "fail"] == []


def test_standing_approvals_are_listed(tmp_path) -> None:
    from app.config.settings import Settings
    from app.core import security_center
    from app.memory.database import Database
    from app.security.grants import GrantStore

    db = Database(tmp_path / "j.db")
    db.initialize()
    text = " ".join(i.text for s in security_center.collect(db, Settings()) if s.title == "Standing approvals" for i in s.items)
    assert "none" in text
    GrantStore(db).add("copy_to_clipboard", minutes=30)
    sections = {s.title: s for s in security_center.collect(db, Settings())}
    assert "copy_to_clipboard" in sections["Standing approvals"].items[0].text and sections["Standing approvals"].items[0].level == "note"
