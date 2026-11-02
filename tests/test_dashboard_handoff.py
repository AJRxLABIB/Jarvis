"""Pure data behind the Dashboard and Handoff screens (the Qt pages only draw these)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core import a11y
from app.ui.command_center import dashboard_data as dd
from app.ui.command_center import handoff_data as hd
from app.ui.command_center import style
from tests.test_handoff import world  # noqa: F401  (fixture)
from tests.test_v3210 import db  # noqa: F401  (fixture)


def test_unknown_stays_unknown():
    cards = {c.key: c for c in dd.build(state_text="")}
    assert cards["ready"].headline == "state not known yet"
    assert cards["devices"].headline == "not known yet" and cards["devices"].tone == "warn"
    assert cards["life"].headline == "not known yet"
    assert "none yet" in cards["actions"].headline


def test_failed_and_uncertain_actions_are_flagged_in_words():
    actions = [{"tool": "open_app", "state": "succeeded"}, {"tool": "phone_link", "state": "uncertain"}, {"tool": "x", "state": "running"}]
    c = {c.key: c for c in dd.build(state_text="Ready", actions=actions)}["actions"]
    assert c.tone == "bad" and any("UNCERTAIN" in line for line in c.lines) and not any("running" in line for line in c.lines)


def test_emergency_and_device_cards():
    dev = [SimpleNamespace(name="Pixel", connection="connected"), SimpleNamespace(name="PC", connection="local"), SimpleNamespace(name="Old", connection="offline")]
    cards = {c.key: c for c in dd.build(state_text="Ready", devices=dev, emergency=["gestures"], pending_approval="Delete file (high risk)")}
    assert cards["ready"].tone == "bad" and "gestures" in cards["ready"].lines[-1]
    assert cards["devices"].headline.startswith("2 devices")
    assert cards["approvals"].tone == "warn" and dd.text([]) == ""


def test_mission_card_shows_uncertain_steps():
    m = {"title": "Tidy", "progress": "1/3", "uncertain": ["Send file"]}
    c = {c.key: c for c in dd.build(state_text="Ready", mission=m)}["mission"]
    assert c.tone == "warn" and "Uncertain: Send file" in c.lines


def test_forest_palette_contrast():
    p = style.palette("forest")
    assert p is style.PALETTES["forest"]
    assert not [(a, b) for a, b in style.CONTRAST_PAIRS if a11y.contrast(p[a], p[b]) < 4.5]


def test_handoff_rows_real_state(world):
    from app.core import handoff as H

    db, registry, phone, ex, store, mission = world
    assert hd.collect(db) == []
    pkg = H.create(db, mission, phone.device_id, registry=registry)
    now = datetime.now(timezone.utc)
    rows = hd.collect(db, device_names={phone.device_id: "Pixel"}, mission_titles={mission.id: "Prepare submission"}, now=now)
    assert len(rows) == 1 and rows[0].state == "issued" and rows[0].id == pkg["id"]
    assert rows[0].line().startswith("Prepare submission -> Pixel: ISSUED (") and "once" in rows[0].note
    late = hd.collect(db, now=now + timedelta(hours=2))
    assert late[0].state == "expired" and late[0].left == "" and "Create a new one" in late[0].note
    H.revoke(db, pkg["id"])
    assert hd.collect(db, now=now)[0].state == "revoked"
