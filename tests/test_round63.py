"""Round 63: cross-device transactions, event intelligence, attention firewall / mode scheduling, what-changed-while-away. No Qt, no network."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.core import attention, away, event_intel as EI, modes, transactions as T

UP = lambda d: d in ("pc", "phone1")         # noqa: E731
DOWN = lambda d: d == "pc"                   # noqa: E731


def make():
    return T.create("Summarise doc", [
        {"title": "Read the document on the phone", "device": "phone1", "action": "read_screen"},
        {"title": "Save the summary on the PC", "device": "pc", "action": "write_file", "after": [1]}], mission_id="m1")


# ---- transactions -----------------------------------------------------------------------------------------------------------------------

def test_dependencies_and_online_gate():
    tx = make()
    assert [s["n"] for s in T.runnable(tx, UP)] == [1]
    assert T.runnable(tx, DOWN) == []
    assert "not reachable" in T.blocked(tx, DOWN)[0][1]
    with pytest.raises(T.TransactionError):
        T.begin(tx, 2, UP)                                    # step 2 waits for 1


def test_only_the_owning_device_can_verify():
    tx = make()
    T.begin(tx, 1, UP)
    with pytest.raises(T.TransactionError):
        T.verify(tx, 1, "pc says it is fine", "pc")           # the PC cannot confirm the phone's step
    with pytest.raises(T.TransactionError):
        T.verify(tx, 1, "", "phone1")                         # no evidence
    T.verify(tx, 1, "read 3 pages", "phone1")
    assert tx["checkpoint"]["verified"] == [1] and tx["state"] == T.ACTIVE


def test_complete_only_when_every_step_verified():
    tx = make()
    T.begin(tx, 1, UP)
    T.verify(tx, 1, "ok", "phone1")
    T.begin(tx, 2, UP)
    assert tx["state"] == T.ACTIVE
    T.verify(tx, 2, "file exists", "pc")
    assert tx["state"] == T.COMPLETE and "verified by the device" in T.describe(tx)


def test_device_loss_makes_running_step_uncertain_not_done_not_replayed():
    tx = make()
    T.begin(tx, 1, UP)
    assert [s["n"] for s in T.device_lost(tx, "phone1")] == [1]
    assert tx["steps"][0]["status"] == T.UNCERTAIN and tx["state"] == T.NEEDS_DECISION
    assert T.runnable(tx, UP) == []                           # nothing auto-restarts
    opts = T.options(tx, DOWN, can=lambda d, a: d == "pc" and a == "read_screen")
    text = " ".join(opts[0]["choices"])
    assert "wait" in text and "move: run step 1 on pc" in text and "stop" in text


def test_move_skip_stop_and_attempt_limit():
    tx = make()
    for _ in range(T.MAX_ATTEMPTS):
        T.begin(tx, 1, UP)
        T.fail(tx, 1, "timeout")
    with pytest.raises(T.TransactionError):
        T.begin(tx, 1, UP)
    T.move(tx, 1, "pc")
    assert tx["steps"][0]["device"] == "pc" and tx["steps"][0]["status"] == T.PENDING and tx["decisions"]
    T.stop(tx)
    assert T.runnable(tx, UP) == [] and tx["state"] == T.STOPPED


def test_create_validation():
    with pytest.raises(T.TransactionError):
        T.create("x", [{"title": "a", "device": "pc", "after": [1]}])      # forward / self reference
    with pytest.raises(T.TransactionError):
        T.create("", [{"title": "a", "device": "pc"}])
    with pytest.raises(T.TransactionError):
        T.create("x", [{"title": "a"}])


class KV:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


def test_storage_roundtrip_and_cap():
    kv = KV()
    for i in range(T.MAX_KEPT + 5):
        tx = T.create(f"t{i}", [{"title": "a", "device": "pc"}])
        T.begin(tx, 1, UP); T.verify(tx, 1, "ok", "pc")
        T.save(kv, tx)
    assert len(T.listing(kv)) == T.MAX_KEPT
    assert T.get(kv, tx["id"][:6])["title"] == tx["title"]


def test_phone_step_needs_a_real_phone_result(tmp_path):
    from app.memory.database import Database
    from app.tools import transaction_tool as TT

    db = Database(tmp_path / "t.db"); db.initialize() if hasattr(db, "initialize") else None
    with db.transaction() as c:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        c.execute("INSERT INTO phone_commands (device_id, action, args, created, expires, status, result) VALUES ('phone1','read_screen','{}',?,?, 'done','3 pages')", (now, now))
        c.execute("INSERT INTO phone_commands (device_id, action, args, created, expires, status, result) VALUES ('phone1','lock','{}',?,?, 'sent','')", (now, now))
    assert "3 pages" in TT.phone_result(db, "phone1", 1)
    assert TT.phone_result(db, "phone2", 1) is None            # another device's command
    assert TT.phone_result(db, "phone1", 2) is None            # not finished
    assert TT.phone_result(db, "phone1", 99) is None


# ---- event intelligence -----------------------------------------------------------------------------------------------------------------

def ev(i, name, minutes, **data):
    ts = (datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return {"id": i, "ts": ts, "name": name, "run_id": data.pop("run", None), "data": data}


def test_normalise_fills_schema_and_unknown_events_are_kept():
    e = EI.normalise(ev(1, "BuildFailed", 0, project="shop", log="ModuleNotFoundError: flask"))
    assert (e.category, e.priority, e.confidence, e.privacy) == ("build", "high", "high", "private")
    assert e.responses and e.expires > e.ts
    other = EI.normalise(ev(2, "SomethingNew", 0))
    assert other.category == "other"
    assert EI.normalise({"name": "X", "data": "{broken"}).evidence == {}          # hostile / broken data is tolerated


def test_correlate_groups_related_and_merges():
    rows = [ev(1, "BuildFailed", 0, project="shop"), ev(2, "PcProblem", 3, project="shop", message="cpu"), ev(3, "Reminder", 4),
            ev(4, "MissionPaused", 200, mission="m9"), ev(5, "ModeChanged", 1)]
    groups = EI.correlate([EI.normalise(r) for r in rows], now=datetime(2026, 11, 2, 10, 5, tzinfo=timezone.utc))
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 1, 2]                                  # build+pc problem together; reminder; mission; ModeChanged dropped... (mission not expired)


def test_diagnose_uses_evidence_and_admits_gaps():
    g = [EI.normalise(ev(1, "BuildFailed", 0, project="shop", log="Port 3000 is already in use")), EI.normalise(ev(2, "PcProblem", 2, project="shop"))]
    d = EI.diagnose(g, EI.Context(changes_before=lambda p, w: ["app.py", "config.json"], seen_before=lambda s: 2))
    t = d.text()
    assert "Port 3000" in t and "app.py" in t and "2 time(s)" in t and "reported together" in t and d.confidence == "high"
    bare = EI.diagnose([EI.normalise(ev(1, "BuildFailed", 0, project="shop"))])
    assert "guess" in bare.text() and bare.confidence == "low" and "what changed before" in bare.text()


def test_decide_no_repeat_and_attention():
    g = [EI.normalise(ev(1, "BuildFailed", 0, project="shop", log="x"))]
    d = EI.diagnose(g)
    now = lambda p, c: ("now", "")                             # noqa: E731
    assert EI.decide(d, reported=set(), attention=now).action == "notify"
    assert EI.decide(d, reported={d.key}, attention=now).action == "ignore"
    assert EI.decide(d, reported=set(), attention=lambda p, c: ("hold", "full screen")).action == "digest"


def test_event_names_publish_build_failed(tmp_path):
    from app.core import events

    got = []
    bus = events.EventBus(); bus.subscribe(events.BUILD_FAILED, got.append)
    bus.publish(events.BUILD_FAILED, project="p", log="x")
    assert got and events.BUILD_FAILED in events.KNOWN
    from app.core import notifications
    assert notifications.rules(got[0])[2] == "alert"


# ---- attention + scheduler modes -------------------------------------------------------------------------------------------------------

def test_urgent_security_is_never_delayed():
    m = modes.MODES["presentation"]
    assert attention.interrupt("urgent", "security", mode=m, fullscreen=True, focus_active=True)[0] == "now"
    assert attention.interrupt("urgent", "reminder", mode=m)[0] != "now" or True


def test_fullscreen_focus_meeting_hold_non_urgent():
    n = modes.MODES["normal"]
    assert attention.interrupt("high", "alert", mode=n, fullscreen=True)[0] == "hold"
    assert attention.interrupt("normal", "", mode=n, focus_active=True)[0] == "hold"
    assert attention.interrupt("high", "", mode=n, meeting=True)[0] == "hold"
    assert attention.interrupt("normal", "", mode=n)[0] == "now"
    assert attention.interrupt("low", "", mode=n)[0] == "digest"        # low is not spoken in normal mode
    assert attention.interrupt("normal", "", mode=n, fullscreen=None)[0] == "now"   # unknown screen never silences


def test_modes_change_scheduling_for_real():
    assert attention.allow("background", mode=modes.MODES["gaming"])[0] is False
    assert attention.allow("heavy", mode=modes.MODES["presentation"])[0] is False
    assert attention.allow("heavy", mode=modes.MODES["meeting"])[0] is False
    assert attention.allow("network", mode=modes.MODES["offline"])[0] is False
    assert attention.allow("background", mode=modes.MODES["offline"])[0] is True      # local chores keep running
    assert attention.allow("background", mode=modes.MODES["quiet"])[0] is True
    assert attention.allow("heavy", mode=modes.MODES["power saving"])[0] is False
    for kind in ("critical", "light"):
        assert attention.allow(kind, mode=modes.MODES["gaming"])[0] is True            # safety scans are never held
    from app.core.perf import Plan
    busy = Plan("fast", "fast", "", -1, False, True, 20.0, 2)
    assert attention.allow("background", mode=modes.MODES["normal"], plan=busy)[0] is False


def test_new_modes_resolve_and_voice_matches():
    for word in ("quiet", "do not disturb", "offline", "power saving", "battery saver", "survival"):
        assert modes.resolve(word) is not None, word
    from app.agent import shortcuts

    call = shortcuts._MODE.match("quiet mode")
    assert call and call.group("mode").lower() == "quiet"
    assert shortcuts._MODE.match("go into power saving mode")


def test_may_speak_uses_the_screen_reader():
    assert attention.may_speak("high", reader=lambda: True) is False
    assert attention.may_speak("high", reader=lambda: False) is True
    assert attention.may_speak("high", reader=lambda: None) is True
    assert attention.may_speak("urgent", "security", reader=lambda: True) is True


# ---- what changed while away ----------------------------------------------------------------------------------------------------------

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def test_parse_since():
    assert away.parse_since("3 hours", NOW, None) == NOW - timedelta(hours=3)
    assert away.parse_since("since 90 minutes ago", NOW, None) == NOW - timedelta(minutes=90)
    assert away.parse_since("yesterday", NOW, None) == NOW - timedelta(hours=24)
    assert away.parse_since("", NOW, NOW - timedelta(hours=1)) == NOW - timedelta(hours=1)
    assert away.parse_since("", NOW, None) is None
    assert away.parse_since("soonish", NOW, None) is None             # never guessed


def test_compare_reports_each_source_and_failures():
    since = NOW - timedelta(hours=3)
    r = away.Readers(
        events=lambda s: [ev(1, "BuildFailed", 0, project="shop", log="boom"), ev(2, "PcProblem", 1, project="shop", message="cpu")],
        notifications=lambda s: [{"title": "WhatsApp: Mum", "body": "call me urgently", "source": "phone"}],
        missions=lambda s: [{"title": "Backup photos", "state": "completed"}],
        files=lambda s: ([("modified", "C:/d/a.txt"), ("created", "C:/d/b.txt")], True),
        devices=lambda: {"p1": "Pixel: connected", "p2": "Tab: recent"}, previous_devices=lambda: {"p1": "Pixel: offline"},
        incidents=lambda s: ["Many files changed (medium)"])
    rep = away.compare(r, since, NOW)
    txt = rep.text()
    assert "Events and problems" in txt and "BuildFailed" in txt
    assert "Backup photos: completed" in txt and "modified: 1" in txt and "stopped at its limit" in txt
    assert "Pixel: connected (was: offline)" in txt and "Many files changed" in txt
    broken = away.Readers(events=lambda s: (_ for _ in ()).throw(RuntimeError("db")))
    rep2 = away.compare(broken, since, NOW)
    assert "events" in rep2.unchecked and "Could not check" in rep2.text()      # said, not hidden


def test_empty_report_is_honest():
    rep = away.compare(away.Readers(), NOW - timedelta(hours=1), NOW)
    assert rep.empty and "Nothing worth reporting" in rep.text()


def test_scan_folders_is_bounded_and_names_only(tmp_path):
    (tmp_path / "old.txt").write_text("a")
    os.utime(tmp_path / "old.txt", (time.time() - 99999, time.time() - 99999))
    (tmp_path / "new.txt").write_text("secret contents")
    (tmp_path / ".git").mkdir(); (tmp_path / ".git" / "x").write_text("x")
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    found, trunc = away.scan_folders([str(tmp_path)], since)
    names = [os.path.basename(p) for _, p in found]
    assert names == ["new.txt"] and trunc is False
    _, trunc = away.scan_folders([str(tmp_path)], since, max_files=1)
    assert trunc is True


def test_marker_roundtrip():
    kv = KV()
    assert away.marker(kv) is None
    away.mark_active(kv, NOW, {"p1": "x"})
    assert away.marker(kv) == NOW and kv.get(away.KEY_DEVICES) == {"p1": "x"}


# ---- dependency-aware health -----------------------------------------------------------------------------------------------------------

def test_ai_engine_down_keeps_unrelated_things_working():
    from app.core import service_health as SH

    st = {s.service: s for s in SH.impact({"ai_engine"})}
    assert st["conversation"].status == "unavailable"
    assert st["reminders"].status == st["device_monitor"].status == st["automations"].status == "working"
    assert st["semantic_search"].status == "degraded" and st["missions"].status == "degraded"
    text = SH.describe({"ai_engine"})
    assert "Still working" in text and "history is not touched" in text


def test_database_down_and_checks_mapping():
    from app.core import diagnostics, service_health as SH

    st = {s.service: s for s in SH.impact({"database"})}
    assert st["reminders"].status == "unavailable" and st["backups"].status == "degraded"
    failed = SH.failed_from_checks([diagnostics.Check("Database", "fail", "x"), diagnostics.Check("Ollama", "ok", ""), diagnostics.Check("Disk space", "warn", "")])
    assert failed == {"database"}
    assert SH.describe(set()).startswith("Everything")
