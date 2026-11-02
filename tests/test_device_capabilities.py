"""Declared vs granted vs demonstrated phone capabilities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core import device_capabilities as dc

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
SEEN = (NOW - timedelta(minutes=1)).isoformat()


def rows(state, history=None, actions=("ring", "send_sms", "lock_phone", "share_screen", "tap_text")):
    return {r.action: r for r in dc.build(actions, state, history or {}, NOW)}


def test_allowed_but_unproven_is_ready_not_working():
    r = rows({"seen": SEEN, "app_version": "1.4", "control": True, "comms": True, "lock": True})
    assert r["ring"].verdict == "ready" and r["ring"].demonstrated == "never"


def test_only_a_recent_success_counts_as_working():
    hist = {"ring": ("done", (NOW - timedelta(days=2)).isoformat()), "tap_text": ("done", (NOW - timedelta(days=90)).isoformat())}
    r = rows({"seen": SEEN, "app_version": "1.4", "control": True}, hist)
    assert r["ring"].verdict == "working"
    assert r["tap_text"].demonstrated == "stale" and r["tap_text"].verdict == "ready"


def test_missing_permission_blocks_with_the_exact_fix_and_an_alternative():
    r = rows({"seen": SEEN, "app_version": "1.4", "control": True, "comms": False, "lock": False})
    assert r["send_sms"].verdict == "blocked" and "Allow calls and messages" in r["send_sms"].detail
    assert r["lock_phone"].verdict == "blocked" and "Device admin" in r["lock_phone"].detail
    assert "Instead" in dc.explain(r["lock_phone"]) and dc.explain(r["ring"]) == ""


def test_control_off_blocks_everything():
    r = rows({"seen": SEEN, "app_version": "1.4", "control": False, "comms": True, "lock": True})
    assert {x.verdict for x in r.values()} == {"blocked"}


def test_old_app_answer_means_unsupported():
    r = rows({"seen": SEEN, "app_version": "1.2", "control": True}, {"tap_text": ("unsupported", NOW.isoformat())})
    assert r["tap_text"].verdict == "unsupported" and not r["tap_text"].declared


def test_a_phone_that_stopped_checking_in_is_offline_even_if_proven():
    state = {"seen": (NOW - timedelta(hours=3)).isoformat(), "app_version": "1.4", "control": True}
    r = rows(state, {"ring": ("done", NOW.isoformat())})
    assert r["ring"].verdict == "offline"


def test_consent_each_time_and_failure_are_reported_honestly():
    r = rows({"seen": SEEN, "app_version": "1.4", "control": True}, {"tap_text": ("failed", NOW.isoformat())})
    assert r["share_screen"].verdict == "needs_you"
    assert r["tap_text"].verdict == "failing"


def test_never_reported_phone_is_offline_not_assumed_ready():
    assert rows({})["ring"].verdict == "offline"


def test_history_comes_from_the_existing_commands_table(tmp_path):
    from app.memory.database import Database
    from app.remote.phone_link import PhoneLink

    db = Database(tmp_path / "a.db")
    db.initialize()
    link = PhoneLink(db)
    link.collect("p1", {"control": True, "app_version": "1.4"})
    cid = link.queue("p1", "ring", {})
    link.collect("p1")                                   # the phone takes it
    assert link.report("p1", cid, True, "ringing")
    assert dc.history_from_db(db, "p1")["ring"][0] == "done"
    assert dc.summary(dc.build(("ring",), {**link.state("p1")}, dc.history_from_db(db, "p1"))) == "1 working"
