"""Round 65: privacy zones (+ enforcement points), guardian USB / file-integrity detectors, adaptive interaction advice."""
from __future__ import annotations

import pytest

from app.core import guardian as G, interaction as I, privacy_zones as Z


class KV:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


@pytest.fixture(autouse=True)
def _reset():
    Z.configure(KV())
    Z.FACTS = None
    Z.reset_cache()
    yield
    Z.FACTS = None
    Z.configure(KV())
    Z.reset_cache()


# ---- privacy zones ---------------------------------------------------------------------------------------------------------------------

def test_builtin_zones_match_programs_and_titles():
    cfg = Z.Config()
    assert Z.blocked(cfg, Z.Facts("KeePassXC.exe", "Database"), "memory")
    assert Z.blocked(cfg, Z.Facts("chrome", "Sign in - Online Banking"), "capture")
    assert Z.blocked(cfg, Z.Facts("msedge", "New tab - InPrivate"), "memory")
    assert Z.blocked(cfg, Z.Facts("chrome", "News"), "memory") is None
    assert Z.blocked(cfg, Z.Facts("zoom", "Zoom", in_meeting=True), "remote")


def test_private_browsing_does_not_restrict_clipboard_or_remote():
    cfg = Z.Config()
    f = Z.Facts("firefox", "Private Browsing")
    assert Z.blocked(cfg, f, "memory") and Z.blocked(cfg, f, "clipboard") is None and Z.blocked(cfg, f, "remote") is None


def test_custom_zone_roundtrip_and_validation():
    kv = KV()
    assert "added" in Z.add_custom(kv, "Crypto wallet", ["wallet"], ["veracrypt.exe"], ["capture", "bogus"])
    cfg = Z.load(kv)
    assert Z.blocked(cfg, Z.Facts("veracrypt", ""), "capture") and Z.blocked(cfg, Z.Facts("veracrypt", ""), "memory") is None
    for bad in (("x", ["abc"], []), ("ok name", [], []), ("ok name", ["ab"], []), ("ok name", ["<script>"], [])):
        with pytest.raises(ValueError):
            Z.add_custom(kv, bad[0], bad[1], bad[2], [])
    assert Z.remove_custom(kv, "crypto wallet") and Z.load(kv).custom == []


def test_builtin_can_be_switched_off_and_on():
    kv = KV()
    Z.set_builtin(kv, "banking", False)
    assert Z.blocked(Z.load(kv), Z.Facts("chrome", "Online Banking"), "capture") is None
    Z.set_builtin(kv, "banking", True)
    assert Z.blocked(Z.load(kv), Z.Facts("chrome", "Online Banking"), "capture")
    with pytest.raises(ValueError):
        Z.set_builtin(kv, "everything", False)


def test_running_check_uses_facts_and_never_raises():
    Z.FACTS = lambda: Z.Facts("bitwarden", "Vault")
    assert Z.allows("memory") is False and "Password managers" in Z.restriction("memory")
    Z.reset_cache()
    Z.FACTS = lambda: Z.Facts("code", "main.py")
    assert Z.allows("memory") is True
    Z.reset_cache()

    def boom():
        raise RuntimeError("x")
    Z.FACTS = boom
    assert Z.restriction("capture") is None             # cannot read: no zone (the existing window block still applies)


def test_describe_states_when_foreground_is_unknown():
    text = Z.describe(Z.Config(), Z.Facts(known=False))
    assert "could not be read" in text and "Banking" in text


def test_capture_tools_are_blocked_inside_a_zone():

    from app.agent.tool_manager import CAPTURE_TOOLS
    assert "take_screenshot" in CAPTURE_TOOLS
    src = open("app/agent/tool_manager.py", encoding="utf-8").read()
    assert 'privacy_zones.restriction("capture")' in src


def test_enforcement_points_are_wired():
    conv = open("app/agent/conversation.py", encoding="utf-8").read()
    assert conv.count('privacy_zones.allows("memory")') >= 5
    assert 'privacy_zones.allows("clipboard")' in open("app/ui/controller_parts/background.py", encoding="utf-8").read()
    srv = open("app/remote/server.py", encoding="utf-8").read()
    assert 'privacy_zones.restriction("remote")' in srv and "privacy_zone" in srv


def test_zone_tool_status_and_edit(tmp_path):

    from app.memory.database import Database
    from app.tools import privacy_zone_tool as PT
    from app.tools.base import ToolContext

    db = Database(tmp_path / "t.db")
    db.initialize() if hasattr(db, "initialize") else None

    class S: pass
    ctx = ToolContext(settings=S(), database=db)
    r = PT.privacy_zones(PT.ZoneArgs(action="add", name="Vault app", titles=["vaultx"]), ctx)
    assert r.ok and "Vault app" in PT.privacy_zones(PT.ZoneArgs(action="status"), ctx).message
    hit = PT.privacy_zones(PT.ZoneArgs(action="test", process="x", title="VaultX - main"), ctx)
    assert "Vault app" in hit.message
    assert "Not in any zone" in PT.privacy_zones(PT.ZoneArgs(action="test", process="code", title="main.py"), ctx).message
    assert "OFF" in PT.privacy_zones(PT.ZoneArgs(action="turn_off", name="banking"), ctx).message
    from app.tools.base import ToolError
    with pytest.raises(ToolError):
        PT.privacy_zones(PT.ZoneArgs(action="add", name="bad", titles=["ab"]), ctx)
    assert PT._risk(PT.ZoneArgs(action="turn_off", name="banking"), ctx).name != "SAFE"       # switching a zone off asks first


# ---- guardian: USB + integrity ---------------------------------------------------------------------------------------------------------

def test_usb_first_look_then_new_drive():
    found, remember = G.usb_changes(None, [("E:|FAT32|16GB|AB12", "E: (FAT32, 16 GB)")])
    assert found == [] and len(remember) == 1
    found, remember = G.usb_changes(remember, [("E:|FAT32|16GB|AB12", "E:"), ("F:|NTFS|64GB|CD34", "F: (NTFS, 64 GB)")])
    assert len(found) == 1 and found[0].kind == "usb_device" and "F:" in found[0].title and "not here at the last look" in found[0].evidence[1]
    assert "contents are never read" in " ".join(found[0].not_seen)
    again, _ = G.usb_changes(remember, [("E:|FAT32|16GB|AB12", "E:")])
    assert again == []


def test_integrity_detects_change_removal_and_keeps_no_content():
    base_now = {"C:/h/hosts": ("aaa", 10, 1.0), "C:/h/cfg": ("bbb", 5, 1.0)}
    found, base = G.integrity_changes(None, base_now)
    assert found == [] and base["C:/h/hosts"][0] == "aaa"
    changed = {"C:/h/hosts": ("zzz", 12, 2.0), "C:/h/cfg": ("bbb", 5, 1.0)}
    found, _ = G.integrity_changes(base, changed)
    assert len(found) == 1 and "hosts" in found[0].title and "10 -> 12" in " ".join(found[0].evidence)
    found, kept = G.integrity_changes(base, {"C:/h/cfg": ("bbb", 5, 1.0)}, missing=["C:/h/hosts"])
    assert len(found) == 1 and "gone" in found[0].title and "C:/h/hosts" in kept
    assert "content" not in " ".join(found[0].evidence).replace("the content hash differs", "")


def test_read_integrity_hashes_real_files_and_reports_missing(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    present, gone = G.read_integrity([str(f), str(tmp_path / "nope.txt")])
    import hashlib
    assert present[str(f)][0] == hashlib.sha256(b"hello").hexdigest() and gone == [str(tmp_path / "nope.txt")]


def make_guardian(drives, integrity):
    from datetime import datetime, timezone

    store = G.Store(KV())
    readers = G.Readers(lambda: None, lambda: [], lambda: [], lambda: {}, lambda: datetime(2026, 11, 3, tzinfo=timezone.utc), lambda: True, drives=drives, integrity=integrity)
    return G.Guardian(store, readers, clock=lambda: datetime(2026, 11, 3, tzinfo=timezone.utc)), store


def test_scan_reports_new_drive_and_changed_file_then_accepts():
    state = {"drives": [("E:|X|1GB", "E:")], "hash": "aaa"}
    g, store = make_guardian(lambda: list(state["drives"]), lambda: ({"C:/hosts": (state["hash"], 1, 1.0)}, []))
    assert g.scan() == []                                               # first look only records
    state["drives"].append(("F:|X|2GB", "F:"))
    state["hash"] = "bbb"
    kinds = sorted(i.kind for i in g.scan())
    assert kinds == ["file_changed", "usb_device"]
    assert g.scan() == []                                               # not reported twice (quiet period)
    g.accept_current()
    assert g.scan() == []


# ---- adaptive interaction --------------------------------------------------------------------------------------------------------------

def test_meeting_rules_out_voice_and_speech():
    a = I.advise(I.Context(in_meeting=True, phone_connected=True, mic_available=True))
    assert a.speak_replies is False and a.ranked[0][0] == "keyboard" and any(c == "voice" for c, _ in a.unavailable)


def test_normal_desk_prefers_voice_unless_typing():
    assert I.advise(I.Context(mic_available=True)).ranked[0][0] == "voice"
    assert I.advise(I.Context(mic_available=True, typing_recent=True)).ranked[0][0] == "keyboard"


def test_fullscreen_prefers_phone_and_missing_things_are_explained():
    a = I.advise(I.Context(fullscreen=True, phone_connected=True, mic_available=False))
    assert a.ranked[0][0] == "phone"
    why = dict(a.unavailable)
    assert "no microphone" in why["voice"] and "switched off" in why["gestures"]
    b = I.advise(I.Context(phone_connected=False, mic_muted=True))
    assert [c for c, _ in b.ranked] == ["keyboard"] and "muted" in dict(b.unavailable)["voice"]


def test_quiet_modes_silence_replies_and_unknown_is_not_no():
    assert I.advise(I.Context(mode="sleep")).speak_replies is False
    a = I.advise(I.Context(gestures_enabled=True, camera_available=None))
    assert any(c == "gestures" and "unknown" in w for c, w in a.ranked)
    assert "Replies:" in a.text()
