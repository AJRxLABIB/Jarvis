"""Round 64: git context, meeting detector, phone-verified transactions, guardian attribution, retention, app profiles, Office / VS Code adapters, panels."""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import app_profiles, attention, event_intel as EI, gitinfo, guardian, meeting, office_info, retention, transactions as T

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


# ---- git context ----------------------------------------------------------------------------------------------------------------------

def fake_run(table):
    def run(argv, cwd):
        for key, (rc, out) in table.items():
            if key in " ".join(argv):
                return rc, out
        return 1, ""
    return run


def test_changes_since_merges_commits_and_uncommitted_and_is_read_only():
    calls = []

    def run(argv, cwd):
        calls.append(argv)
        if argv[1] == "log":
            return 0, "app.py\n\nconfig.json\napp.py\n"
        return 0, " M app.py\n?? new.txt\n"
    got = gitinfo.changes_since(run, Path("."), NOW)
    assert got == ["app.py", "config.json", "app.py (uncommitted)", "new.txt (uncommitted)"]
    assert {a[1] for a in calls} == {"log", "status"}                      # only reads


def test_changes_since_not_a_repo_or_error_is_empty():
    assert gitinfo.changes_since(lambda a, c: (128, "fatal: not a git repository"), Path("."), NOW) == []

    def boom(a, c):
        raise RuntimeError("no git")
    assert gitinfo.changes_since(boom, Path("."), NOW) == []
    assert gitinfo.last_commits(boom, Path(".")) == []


def test_diagnosis_uses_git_context_and_prefers_the_folder():
    seen = {}

    def reader(where, when):
        seen["where"] = where
        return ["app.py"]
    ev = EI.normalise({"id": 1, "ts": NOW.isoformat(), "name": "BuildFailed", "run_id": None,
                       "data": {"project": "shop", "folder": "C:/dev/shop", "log": "boom"}})
    d = EI.diagnose([ev], EI.Context(changes_before=reader))
    assert seen["where"] == "C:/dev/shop" and "app.py" in d.text()


def test_event_context_resolves_projects(tmp_path):
    ctx = gitinfo.event_context(lambda p: tmp_path if p == "shop" else None, lambda a, c: (0, "x.py\n" if a[1] == "log" else ""), lambda s: 0)
    assert ctx.changes_before("shop", NOW) == ["x.py"]
    assert ctx.changes_before("other", NOW) == []


def test_count_similar(tmp_path):
    from app.memory.database import Database

    db = Database(tmp_path / "t.db")
    db.initialize() if hasattr(db, "initialize") else None
    with db.transaction() as c:
        for _ in range(3):
            c.execute("INSERT INTO events (ts, name, run_id, data) VALUES ('2026-01-01T00:00:00','BuildFailed',NULL,?)", (json.dumps({"project": "shop"}),))
        c.execute("INSERT INTO events (ts, name, run_id, data) VALUES ('2026-01-01T00:00:00','BuildFailed',NULL,?)", (json.dumps({"project": "other"}),))
    assert gitinfo.count_similar(db, "build:shop:BuildFailed") == 2
    assert gitinfo.count_similar(db, "garbage") == 0


# ---- meeting detector -----------------------------------------------------------------------------------------------------------------

def test_meeting_needs_more_than_the_microphone():
    assert meeting.detect(meeting.Facts("zoom", "Zoom Meeting", True)).in_meeting
    assert meeting.detect(meeting.Facts("chrome", "Standup - meet.google.com", True)).in_meeting
    v = meeting.detect(meeting.Facts("recorder", "Voice", True))
    assert not v.in_meeting and "not treated as a meeting" in v.reason
    assert not meeting.detect(meeting.Facts("zoom", "Zoom Meeting", True, jarvis_listening=True)).in_meeting
    assert not meeting.detect(meeting.Facts("code", "main.py", False)).in_meeting


def test_unknown_microphone_is_never_a_confident_meeting():
    v = meeting.detect(meeting.Facts("teams", "Weekly | Microsoft Teams", None))
    assert v.in_meeting and v.confidence == "low"
    assert not meeting.detect(meeting.Facts("teams", "Chat", None)).in_meeting
    assert not meeting.detect(meeting.Facts("notepad", "a", None)).in_meeting


def test_attention_uses_the_meeting_reader():
    assert attention.may_speak("high", reader=lambda: False, meeting_reader=lambda: True) is False
    assert attention.may_speak("high", reader=lambda: False, meeting_reader=lambda: False) is True
    assert attention.may_speak("urgent", "security", reader=lambda: False, meeting_reader=lambda: True) is True


# ---- phone-verified transactions ------------------------------------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    from app.memory.database import Database

    d = Database(tmp_path / "t.db")
    d.initialize() if hasattr(d, "initialize") else None
    return d


def _tx():
    tx = T.create("Read and save", [{"title": "Read on phone", "device": "phone1", "action": "read_screen"},
                                     {"title": "Save on PC", "device": "pc", "action": "write_file", "after": [1]}])
    T.begin(tx, 1, lambda d: True)
    return tx


def _cmd(db, device, action, status, result="", created=None):
    created = created or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.transaction() as c:
        return c.execute("INSERT INTO phone_commands (device_id, action, args, created, expires, status, result) VALUES (?,?,?,?,?,?,?)",
                         (device, action, "{}", created, created, status, result)).lastrowid


def test_reconcile_verifies_from_the_phones_own_result(db):
    from app.tools.transaction_tool import find_command

    tx = _tx()
    find = lambda d, a, s, c: find_command(db, d, a, s, c)           # noqa: E731
    assert T.reconcile(tx, find) == []                               # nothing sent yet
    cid = _cmd(db, "phone1", "read_screen", "sent")
    assert T.reconcile(tx, find) == [] and tx["steps"][0]["status"] == T.RUNNING        # sent is not done
    with db.transaction() as c:
        c.execute("UPDATE phone_commands SET status='done', result='3 pages' WHERE id=?", (cid,))
    assert T.reconcile(tx, find) == ["step 1 verified by phone1"]
    s = tx["steps"][0]
    assert s["status"] == T.VERIFIED and "3 pages" in s["evidence"] and s["command"] == cid


def test_reconcile_ignores_other_devices_older_commands_and_pc_steps(db):
    from app.tools.transaction_tool import find_command

    tx = _tx()
    find = lambda d, a, s, c: find_command(db, d, a, s, c)           # noqa: E731
    _cmd(db, "phone2", "read_screen", "done", "other phone")
    _cmd(db, "phone1", "lock", "done", "wrong action")
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    _cmd(db, "phone1", "read_screen", "done", "before the step began", created=old)
    assert T.reconcile(tx, find) == [] and tx["steps"][0]["status"] == T.RUNNING


def test_reconcile_failed_command_fails_the_step_without_retrying(db):
    from app.tools.transaction_tool import find_command

    tx = _tx()
    _cmd(db, "phone1", "read_screen", "unsupported", "no accessibility")
    out = T.reconcile(tx, lambda d, a, s, c: find_command(db, d, a, s, c))
    assert out == ["step 1 failed (unsupported)"] and tx["state"] == T.NEEDS_DECISION and tx["steps"][0]["attempts"] == 1


def test_reconcile_all_settles_stored_transactions(db):
    from app.memory.settings_store import SettingsStore
    from app.tools.transaction_tool import reconcile_all

    store = SettingsStore(db)
    tx = _tx()
    T.save(store, tx)
    _cmd(db, "phone1", "read_screen", "done", "ok")
    notes = reconcile_all(db)
    assert notes and "verified by phone1" in notes[0]
    assert T.get(store, tx["id"])["steps"][0]["status"] == T.VERIFIED


# ---- guardian attribution -------------------------------------------------------------------------------------------------------------

def test_describe_holders_and_scan_evidence():
    assert guardian.describe_holders({"word.exe": ["C:/d/a.docx", "C:/d/b.docx"], "x.exe": ["C:/d/c"]}) == "word.exe (2 files, e.g. a.docx); x.exe (1 file, e.g. c)"
    assert guardian.describe_holders({}) == ""


# ---- retention -------------------------------------------------------------------------------------------------------------------------

class KV:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


class S:
    clipboard_retention_days = 7
    metrics_retention_days = 14
    memory_event_retention_days = 0


def test_retention_status_and_clear_each_store(db):
    kv = KV()
    kv.d["transactions"] = [{"state": "complete"}, {"state": "active"}]
    kv.d["guardian_incidents"] = [{"status": "open"}, {"status": "dismissed"}]
    with db.transaction() as c:
        c.execute("INSERT INTO events (ts, name, run_id, data) VALUES ('2026-01-01T00:00:00','Reminder',NULL,'{}')")
        c.execute("INSERT INTO notifications (ts, source, category, priority, title, read) VALUES ('2026-01-01T00:00:00','x','system','low','a',1)")
        c.execute("INSERT INTO notifications (ts, source, category, priority, title, read) VALUES ('2026-01-01T00:00:00','x','system','low','b',0)")
    rows = {r.key: r for r in retention.status(db, S(), kv)}
    assert rows["events"].count == 1 and rows["notifications"].count == 2 and rows["transactions"].count == 1 and rows["incidents"].count == 1
    assert "no limit set" in rows["activity"].keeps and "separate copies" in retention.describe(list(rows.values()))
    n, note = retention.clear("notifications", db, S(), kv)
    assert n == 1 and "unread ones were kept" in note
    assert retention.clear("events", db, S(), kv)[0] == 1
    assert retention.clear("transactions", db, S(), kv)[0] == 1 and kv.d["transactions"] == [{"state": "active"}]
    assert retention.clear("incidents", db, S(), kv)[0] == 1 and kv.d["guardian_incidents"] == [{"status": "open"}]
    for k in ("clipboard", "metrics", "activity"):
        assert isinstance(retention.clear(k, db, S(), kv)[0], int)
    with pytest.raises(ValueError):
        retention.clear("everything", db, S(), kv)


# ---- app profiles ---------------------------------------------------------------------------------------------------------------------

def test_profiles_give_alternatives_shortcuts_and_notes():
    assert app_profiles.alternatives("NOTEPAD.EXE", "Save") == ["Save As"]
    assert app_profiles.shortcut("winword", "Bold") == "Ctrl+B"
    adv = app_profiles.advice("code", "run")
    assert "F5" in adv and "Electron" in adv
    assert app_profiles.advice("unknownapp", "save") == "" and app_profiles.alternatives("unknownapp", "save") == []
    assert app_profiles.risky_dialog("notepad", "Do you want to save changes to Untitled?")
    assert not app_profiles.risky_dialog("notepad", "Replace file?") and not app_profiles.risky_dialog("zzz", "do you want to save")


def test_press_control_falls_back_to_profile_labels(monkeypatch):
    from app.tools import ui_elements as U, winapi

    class W:
        hwnd, title, process_name = 1, "Untitled - Notepad", "notepad.exe"

    asked = []

    def fake_find(hwnd, wanted):
        asked.append(wanted)
        return [] if wanted == "Save" else [U.Found("Save As", "button", object(), 1.0)]
    monkeypatch.setattr(U, "find_controls", fake_find)
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "target_window", lambda: W())
    import app.tools.screen_agent as SA
    monkeypatch.setattr(SA, "hand_over", lambda ctx: None)
    monkeypatch.setattr(U, "press", lambda e: "invoked")
    monkeypatch.setattr(U, "reacted", lambda *a, **k: "a window opened")
    monkeypatch.setattr(U, "_snapshot", lambda: (set(), {}))
    monkeypatch.setattr(U, "_toggle_state", lambda e: None)
    result = U.press_control(U.PressArgs(name="Save"), None)
    assert asked == ["Save", "Save As"] and "Save As" in result.message


def test_press_control_not_found_adds_advice(monkeypatch):
    from app.tools import ui_elements as U, winapi
    from app.tools.base import ToolError

    class W:
        hwnd, title, process_name = 1, "main.py - VS Code", "Code.exe"
    monkeypatch.setattr(U, "find_controls", lambda h, w: [])
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "target_window", lambda: W())
    import app.tools.screen_agent as SA
    monkeypatch.setattr(SA, "hand_over", lambda ctx: None)
    with pytest.raises(ToolError) as e:
        U.press_control(U.PressArgs(name="Run"), None)
    assert "F5" in str(e.value)


# ---- office + vscode ------------------------------------------------------------------------------------------------------------------

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return path


def test_office_word_excel_powerpoint(tmp_path):
    docx = _zip(tmp_path / "a.docx", {"word/document.xml": f'<w:document xmlns:w="{W}"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Intro</w:t></w:r></w:p>'
                                                         '<w:p><w:r><w:t>hello brave new world</w:t></w:r></w:p><w:tbl/></w:body></w:document>',
                                       "docProps/core.xml": '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                                                            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>Labib</dc:creator></cp:coreProperties>'})
    i = office_info.summarize(docx)
    assert i["words"] == 5 and i["headings"] == ["Heading1: Intro"] and i["tables"] == 1 and i["author"] == "Labib"
    assert "5 words" in office_info.render(i)
    xlsx = _zip(tmp_path / "b.xlsx", {"xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="Budget" sheetId="1"/></sheets></workbook>',
                                       "xl/worksheets/sheet1.xml": '<worksheet><dimension ref="A1:C9"/><sheetData><c><f>SUM(A1:A3)</f></c><c><f>B1</f></c></sheetData></worksheet>'})
    x = office_info.summarize(xlsx)
    assert x["sheets"] == [{"name": "Budget", "range": "A1:C9", "formulas": 2}]
    P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    pptx = _zip(tmp_path / "c.pptx", {"ppt/slides/slide1.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:sp><a:t>Welcome</a:t></p:sp></p:sld>',
                                       "ppt/slides/slide2.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:sp><a:t>Results</a:t></p:sp></p:sld>'})
    assert office_info.summarize(pptx)["titles"] == ["Welcome", "Results"]


def test_office_refuses_macros_wrong_types_and_damage(tmp_path):
    with pytest.raises(office_info.OfficeError):
        office_info.summarize(_zip(tmp_path / "m.docx", {"word/document.xml": "<x/>", "word/vbaProject.bin": "x"}))
    with pytest.raises(office_info.OfficeError):
        office_info.summarize(tmp_path / "notes.txt")
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(office_info.OfficeError):
        office_info.summarize(bad)
    with pytest.raises(office_info.OfficeError):
        office_info.summarize(_zip(tmp_path / "e.docx", {"word/document.xml": "<<<"}))


def test_vscode_arguments_are_fixed_and_paths_checked():
    from app.tools.base import ToolError
    from app.tools.editor_tools import VSCodeArgs, build_argv

    r = lambda t: "C:/p/" + t                                                  # noqa: E731
    assert build_argv(VSCodeArgs(action="goto", path="a.py", line=12, column=3), r) == ["code", "-g", "C:/p/a.py:12:3"]
    assert build_argv(VSCodeArgs(action="folder", path="proj"), r) == ["code", "C:/p/proj"]
    assert build_argv(VSCodeArgs(action="diff", path="a", other="b"), r) == ["code", "--diff", "C:/p/a", "C:/p/b"]
    assert build_argv(VSCodeArgs(action="extensions"), r) == ["code", "--list-extensions"]
    with pytest.raises(ToolError):
        build_argv(VSCodeArgs(action="goto"), r)
    with pytest.raises(ToolError):
        build_argv(VSCodeArgs(action="diff", path="a"), r)
    argv = build_argv(VSCodeArgs(action="folder", path="--install-extension evil"), r)
    assert argv == ["code", "C:/p/--install-extension evil"]                  # a flag-looking string becomes a (checked) path, never a flag


# ---- panels + phone overview ----------------------------------------------------------------------------------------------------------

def test_every_panel_block_and_action_is_a_real_tool_with_valid_arguments():
    from app.tools.catalog import build_default_registry
    from app.ui.command_center import panel_specs as ps

    reg = build_default_registry()
    specs = {s.name: s for s in reg.specs()}
    for panel in ps.PANELS:
        for item in [*panel.blocks, *panel.actions]:
            assert item.tool in specs, f"{panel.key}: {item.tool}"
            reg.get(item.tool).args_model(**item.args)                           # raises if the arguments do not fit
    assert set(ps.RETENTION_STORES) == set(retention.KEYS)
    src = Path("app/ui/command_center/widgets.py").read_text(encoding="utf-8")
    assert all(f'("{p.key}"' in src for p in ps.PANELS)                         # every panel has a rail entry


def test_overview_is_a_read_only_phone_command():
    from app.remote import mobile, server

    assert mobile.COMMANDS["overview"] == ("read", None)
    assert "overview" in server.READ_ONLY_PHONE_COMMANDS
    assert "overview" not in mobile.STEP_UP_COMMANDS


def test_new_tools_are_registered():
    from app.tools.catalog import build_default_registry

    names = {s.name for s in build_default_registry().specs()}
    assert {"vscode", "office_info", "retention"} <= names
