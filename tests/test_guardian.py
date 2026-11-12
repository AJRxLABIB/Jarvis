"""Cyber guardian: detectors, store, a full scan with fake readers, and the tool. No real files, registry or network are read."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core import guardian as G
from app.memory.database import Database
from app.memory.settings_store import SettingsStore

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def files(n, ext=".docx", sig=(10, 1), root="C:/Users/x/Documents"):
    return {f"{root}/f{i}{ext}": sig for i in range(n)}


def test_first_scan_only_builds_a_baseline():
    inc, stats = G.file_burst(None, files(500))
    assert inc is None and stats["modified"] == 0


def test_ordinary_activity_is_not_a_burst():
    before = files(300)
    now = dict(before)
    for i in range(10):
        now[f"C:/Users/x/Documents/f{i}.docx"] = (11, 2)
    now["C:/Users/x/Documents/new.docx"] = (1, 1)
    assert G.file_burst(before, now)[0] is None


def test_ransomware_like_burst_is_high_with_evidence_and_an_honest_caveat():
    before = files(400)
    now = {p.rsplit(".", 1)[0] + ".locked": (50, 9) for p in before}
    inc, stats = G.file_burst(before, now)
    assert inc and inc.severity == G.HIGH and stats["deleted"] == 400 and stats["new_extension"] == 400
    assert any(".locked" in e for e in inc.evidence) and any("legitimate bulk job" in r for r in inc.recommended)
    assert any("never reads" in n for n in inc.not_seen)


def test_mass_modification_alone_is_medium_with_low_confidence():
    before = files(200)
    now = {p: (11, 5) for p in before}
    inc, _ = G.file_burst(before, now)
    assert inc.severity == G.MEDIUM and inc.confidence == "low"


def test_suspects_are_guesses_from_disk_writes():
    before = {1: ("a.exe", 0), 2: ("b.exe", 0)}
    now = {1: ("a.exe", 900 * 1048576), 2: ("b.exe", 100), 3: ("new.exe", 5 * 1048576)}
    assert G.suspects(before, now) == ["a.exe (wrote 900 MB since the last look)"]


def test_startup_changes_report_new_and_changed_entries_and_flag_unusual_places():
    base = [("HKCU\\Run", "Discord", "C:\\Discord\\Update.exe")]
    now = base + [("HKCU\\Run", "Updater", "C:\\Users\\x\\AppData\\Local\\Temp\\u.exe"), ("HKCU\\Run", "Notes", "C:\\Apps\\notes.exe")]
    found = G.startup_changes(base, now)
    by = {i.title.split("'")[1]: i for i in found}
    assert by["Updater"].severity == G.MEDIUM and "unusual place" in " ".join(by["Updater"].evidence) and by["Notes"].severity == G.LOW
    assert G.startup_changes(None, now) == []
    changed = G.startup_changes(base, [("HKCU\\Run", "Discord", "powershell -enc AAAA")])
    assert "changed its startup command" in changed[0].title and changed[0].severity == G.MEDIUM


def test_network_changes_listeners_and_many_destinations():
    rows = [("node.exe", "0.0.0.0", 3000, "", 0, "LISTEN"), ("local.exe", "127.0.0.1", 9000, "", 0, "LISTEN")] + \
           [("odd.exe", "192.168.1.5", 5000 + i, f"8.8.{i}.8", 443, "ESTABLISHED") for i in range(10)]
    incidents, base = G.network_changes(None, rows)
    assert incidents == [] and "node.exe:3000" in base["listen"] and "local.exe:9000" not in base["listen"]
    again, _ = G.network_changes({"listen": [], "dest": []}, rows)
    kinds = {i.kind for i in again}
    assert kinds == {"new_listener", "many_destinations"}
    quiet, _ = G.network_changes(base, rows)
    assert quiet == []


def test_backup_checks():
    assert G.backup_check(NOW, None, None).key == "backup:none"
    assert G.backup_check(NOW, NOW - timedelta(days=1), False).severity == G.HIGH
    assert G.backup_check(NOW, NOW - timedelta(days=9), True).title.startswith("The newest backup is 9 days old")
    assert G.backup_check(NOW, NOW - timedelta(hours=5), True) is None


@pytest.fixture()
def store(tmp_path):
    db = Database(tmp_path / "g.db")
    db.initialize()
    audit = []
    return db, G.Store(SettingsStore(db), lambda event, **kw: audit.append((event, kw)), clock=lambda: NOW), audit


def readers(**over):
    data = dict(files=lambda: files(10), startup=lambda: [], connections=lambda: [], writes=lambda: {}, last_backup=lambda: NOW, restore_ok=lambda: True)
    data.update(over)
    return G.Readers(**data)


def test_store_deduplicates_audits_and_respects_quiet_hours(store):
    db, s, audit = store
    inc = G.Incident("", "", "backup", G.LOW, "high", "t", key="k")
    first = s.add(inc)
    assert first and first.id.startswith("G0001") and audit[0][0] == "guardian_incident"
    assert s.add(G.Incident("", "", "backup", G.LOW, "high", "t", key="k")) is None              # still open: not reported again
    s.set_status(first.id, "dismissed")
    assert audit[-1][1]["details"]["status"] == "dismissed"
    assert s.add(G.Incident("", "", "backup", G.LOW, "high", "t", key="k")) is None              # dismissed recently: quiet
    s.clock = lambda: NOW + timedelta(hours=7)
    assert s.add(G.Incident("", "", "backup", G.LOW, "high", "t", key="k")) is not None          # later it may come back


def test_full_scan_flow_with_fake_readers(store):
    db, s, _ = store
    state = {"files": files(300), "startup": [("HKCU\\Run", "A", "C:\\a.exe")], "conn": []}
    g = G.Guardian(s, readers(files=lambda: state["files"], startup=lambda: state["startup"], connections=lambda: state["conn"]), clock=lambda: NOW)
    assert g.scan() == [] and "No open incidents" in g.summary()                                  # first scan = baseline, silence
    state["files"] = {p: (99, 7) for p in state["files"]}                                         # everything modified
    state["startup"] = state["startup"] + [("HKCU\\Run", "B", "C:\\Users\\x\\Downloads\\b.exe")]
    state["conn"] = [("srv.exe", "0.0.0.0", 8080, "", 0, "LISTEN")]
    new = g.scan()
    kinds = sorted(i.kind for i in new)
    assert kinds == ["file_burst", "new_listener", "startup_change"]
    assert g.scan() == []                                                                          # the same things are not reported twice
    assert "3 open incident(s)" in g.summary()


def test_a_failing_reader_never_breaks_the_scan(store):
    _, s, _ = store

    def boom():
        raise OSError("disk gone")

    g = G.Guardian(s, readers(files=boom, startup=boom, connections=boom), clock=lambda: NOW)
    assert g.scan() == []


def test_accept_baseline_quiets_current_state(store):
    _, s, _ = store
    cur = {"s": [("HKCU\\Run", "A", "C:\\a.exe")]}
    g = G.Guardian(s, readers(startup=lambda: cur["s"]), clock=lambda: NOW)
    g.scan()
    cur["s"] = cur["s"] + [("HKCU\\Run", "B", "C:\\b.exe")]
    assert "startup_change" in [i.kind for i in g.scan()]
    s.set_status(s.incidents()[-1].id, "dismissed")
    assert "baseline" in g.accept_current()


def test_read_files_ignores_missing_folders_and_returns_none_when_nothing_is_readable(tmp_path):
    assert G.read_files([str(tmp_path / "nope")]) is None
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.js").write_text("x")
    snap = G.read_files([str(tmp_path)])
    assert snap is not None and len(snap) == 1


def test_tool_flow_and_notifications(store, tmp_path):
    from app.core.notifications import NotificationCenter
    from app.tools import guardian_tool as T

    db, s, _ = store
    cur = {"s": []}
    g = G.Guardian(s, readers(startup=lambda: cur["s"], last_backup=lambda: None, restore_ok=lambda: None), clock=lambda: NOW)
    G._INSTANCE = g
    try:
        ctx = SimpleNamespace(database=db, settings=SimpleNamespace(cyber_guardian=False), paths=None)
        run = lambda **a: asyncio.run(T.guardian_tool(T.GuardianArgs(**a), ctx))  # noqa: E731
        assert "OFF" in run(action="status").message
        scan = run(action="scan")
        assert scan.data["new"] and "no backup" in scan.message.lower()
        assert NotificationCenter(db).items(category="security")[0].title.startswith("Guardian:")
        iid = scan.data["new"][0]
        assert "evidence" in run(action="show", id=iid).message
        assert "Nothing was stopped" in run(action="acknowledge", id=iid).message
        assert run(action="incidents").message == "No open incidents."
        with pytest.raises(Exception):
            run(action="dismiss", id="nope")
    finally:
        G._INSTANCE = None


def test_guardian_has_no_way_to_act():
    import ast
    import inspect

    src = inspect.getsource(G) + inspect.getsource(__import__("app.tools.guardian_tool", fromlist=["x"]))
    names = {n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Attribute, ast.Name))}
    assert not names & {"kill", "terminate", "remove", "unlink", "rmtree", "Popen", "system", "rename"}
