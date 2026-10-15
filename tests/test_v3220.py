"""3.22: JARVIS Mobile 1.0 - biometric-gated control key, Windows state + auth capability detection, remote
sessions, files/screen handlers, and the new commands. Adversarial cases included."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app.memory.database import Database
from app.remote import mobile, mobile_ops, pc_state, win_auth
from app.remote.mobile import MobileRegistry, SessionStore
from tests.test_v3150 import Phone, code_of, paired


@pytest.fixture()
def registry(tmp_path: Path) -> MobileRegistry:
    database = Database(tmp_path / "j.db")
    database.initialize()
    return MobileRegistry(database)


# ------------------------------------------------------------------ the biometric-gated control key


def test_control_needs_the_auth_key(registry) -> None:
    phone = paired(registry, auth_key=False)  # paired but no protected key yet
    assert registry.verify(phone.envelope("status"))[1] == "status"  # read-only is fine
    registry.set_permissions(phone.device_id, ["read", "power", "control"])
    assert code_of(lambda: registry.verify(phone.envelope("shutdown"))) == "no_auth_key"
    phone.register_auth(registry)
    assert registry.verify(phone.envelope("shutdown"))[1] == "shutdown"


def test_control_signed_with_the_wrong_key_is_refused(registry) -> None:
    phone = paired(registry)
    registry.set_permissions(phone.device_id, ["read", "power"])
    # sign a step-up command with the BASE key instead of the auth key
    forged = phone.envelope("shutdown", key=phone.key)
    assert code_of(lambda: registry.verify(forged)) == "bad_signature"


def test_auth_key_must_prove_itself_and_is_set_once(registry) -> None:
    phone = paired(registry, auth_key=False)
    device = registry.get(phone.device_id)
    wrong = base64.b64encode(ec.generate_private_key(ec.SECP256R1()).sign(b"nope",
                             ec.ECDSA(hashes.SHA256()))).decode()
    assert code_of(lambda: registry.register_auth_key(device, phone.auth_public, wrong)) == "bad_signature"
    phone.register_auth(registry)
    # a second, different key can't silently replace the first
    other = Phone()
    other.device_id = phone.device_id
    assert code_of(lambda: other.register_auth(registry)) == "auth_key_set"


def test_read_only_commands_still_use_the_base_key(registry) -> None:
    phone = paired(registry, auth_key=False)
    assert registry.verify(phone.envelope("status"))[1] == "status"
    assert registry.verify(phone.envelope("capabilities"))[1] == "capabilities"


# ------------------------------------------------------------------ Windows state machine


class FakeSession:
    def __init__(self, state, flags, protocol=1, desktop=True):
        self._state, self._flags, self._protocol, self._desktop = state, flags, protocol, desktop

    def session_info(self):
        return (self._state, self._flags)

    def protocol(self):
        return self._protocol

    def input_desktop_open(self):
        return self._desktop


def test_session_lock_state_is_read_not_guessed() -> None:
    active = pc_state.session(FakeSession(0, pc_state.WTS_SESSIONSTATE_UNLOCK))
    assert active.locked is False and not active.remote
    locked = pc_state.session(FakeSession(0, pc_state.WTS_SESSIONSTATE_LOCK))
    assert locked.locked is True
    rdp = pc_state.session(FakeSession(0, pc_state.WTS_SESSIONSTATE_UNLOCK, protocol=2))
    assert rdp.remote is True


def test_snapshot_never_claims_unlocked_when_locked() -> None:
    pc_state.reset()  # a power test elsewhere may have announced a shutdown; this test is about lock state
    auth = win_auth.AuthCapabilities(edition="Professional", rdp_host_supported=True, rdp_enabled=True, rdp_port=3389)
    locked = pc_state.snapshot(jarvis_ready=True, session_state=pc_state.Session(True, False, False, "test"), auth=auth)
    assert locked["state"] == "AUTHENTICATION_AVAILABLE" and "Remote Desktop is available" in locked["explain"]
    no_rdp = pc_state.snapshot(jarvis_ready=True, session_state=pc_state.Session(True, False, False, "test"),
                               auth=win_auth.AuthCapabilities(edition="Core", rdp_host_supported=False))
    assert no_rdp["state"] == "ONLINE_LOCKED" and "cannot bypass" in no_rdp["explain"]
    ready = pc_state.snapshot(jarvis_ready=True, session_state=pc_state.Session(False, False, True, "test"), auth=auth)
    assert ready["state"] == "READY"
    unknown = pc_state.snapshot(jarvis_ready=True, session_state=pc_state.Session(None, False, None, "x"))
    assert unknown["state"] == "UNKNOWN"


def test_announced_power_is_shown_then_expires() -> None:
    pc_state.reset()
    clock = [1000.0]
    pc_state.announce_power("shutdown", clock=lambda: clock[0])
    snap = pc_state.snapshot(jarvis_ready=True, session_state=pc_state.Session(False, False, True, "x"),
                             clock=lambda: clock[0])
    assert snap["state"] == "SHUTTING_DOWN"
    clock[0] += pc_state.POWER_ANNOUNCE_SECONDS + 1
    later = pc_state.snapshot(jarvis_ready=True, session_state=pc_state.Session(False, False, True, "x"),
                              clock=lambda: clock[0])
    assert later["state"] == "READY"
    pc_state.reset()


# ------------------------------------------------------------------ Windows auth capability detection


class FakeProbe:
    def __init__(self, values, **flags):
        self.values, self.flags = values, flags

    def registry(self, hive, path, name):
        key = (path, name)
        if key in self.values:
            return self.values[key]
        raise FileNotFoundError(name)

    def registry_key_exists(self, hive, path):
        return self.flags.get("pin", False) and path.endswith(self.flags.get("sid", "S-1-5-21-1"))

    def user_sid(self):
        return self.flags.get("sid", "S-1-5-21-1")

    def biometric_units(self, kind):
        return 1 if (kind == win_auth.WINBIO_TYPE_FINGERPRINT and self.flags.get("finger")) else 0

    def tpm_version(self):
        return self.flags.get("tpm", 2)

    def microsoft_account(self):
        return self.flags.get("msa", False)

    def entra_join(self):
        return self.flags.get("entra", 0)


def test_home_edition_reports_remote_desktop_unsupported() -> None:
    probe = FakeProbe({("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "ProductName"): "Windows 10 Home",
                       ("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "EditionID"): "Core",
                       ("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "CurrentBuild"): "22631"})
    caps = win_auth.detect(probe)
    assert caps.rdp_host_supported is False and caps.remote_desktop == "unsupported_edition"
    assert "Windows 11" in caps.windows  # build >= 22000 corrects the name
    assert any(o["method"] == "remote_desktop" and not o["available"] for o in caps.sign_in_options())
    assert caps.remote_unlock()["supported"] is False


def test_pro_edition_with_rdp_on_is_available() -> None:
    probe = FakeProbe({("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "ProductName"): "Windows 11 Pro",
                       ("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "EditionID"): "Professional",
                       ("SYSTEM\\CurrentControlSet\\Control\\Terminal Server", "fDenyTSConnections"): 0},
                      finger=True, pin=True)
    caps = win_auth.detect(probe)
    assert caps.remote_desktop == "available" and caps.fingerprint_sensor is True and caps.tpm == "2.0"
    assert caps.to_dict()["remote_desktop"] == "available"


def test_unreadable_values_are_unknown_not_guessed() -> None:
    caps = win_auth.detect(FakeProbe({}))
    assert caps.edition == "unknown" and caps.rdp_enabled is None and caps.remote_desktop == "unknown"


# ------------------------------------------------------------------ sessions


def test_sessions_open_touch_and_terminate(tmp_path: Path) -> None:
    database = Database(tmp_path / "s.db")
    database.initialize()
    clock = [1000.0]
    store = SessionStore(database, clock=lambda: clock[0])
    s1 = store.open("dev1", ["lock"], network="LAN 192.168.0.5")
    same = store.open("dev1", ["screen"])  # reused, scopes merged
    assert same.id == s1.id and set(same.scopes) == {"lock", "screen"}
    assert len(store.active()) == 1
    store.end(s1.id, "ended from the PC")
    assert store.active() == [] and store.get(s1.id).end_reason == "ended from the PC"


def test_idle_sessions_expire(tmp_path: Path) -> None:
    database = Database(tmp_path / "s.db")
    database.initialize()
    clock = [1000.0]
    store = SessionStore(database, clock=lambda: clock[0])
    store.open("dev1", ["screen"])
    clock[0] += SessionStore.IDLE_SECONDS + 5
    assert store.active() == []


def test_revoking_a_phone_ends_its_sessions(tmp_path: Path) -> None:
    database = Database(tmp_path / "s.db")
    database.initialize()
    store = SessionStore(database)
    store.open("devX", ["lock"])
    assert store.end_device("devX", "revoked") == 1 and store.active() == []


# ------------------------------------------------------------------ files (path policy enforced)


class Ctx:
    def __init__(self, policy, settings=None):
        self._policy = policy
        self.settings = settings

    def get_path_policy(self):
        return self._policy


def _policy(tmp_path: Path):
    from app.config.files import PermissionsConfig
    from app.security.validator import PathPolicy

    safe = tmp_path / "safe"
    safe.mkdir()
    known = {"HOME": tmp_path, "DESKTOP": safe, "DOWNLOADS": safe, "DOCUMENTS": safe}
    return PathPolicy.build(PermissionsConfig(safe_roots=["{DESKTOP}"]), known=known,
                            protected=[tmp_path / "secret"]), safe


def test_files_stay_inside_safe_roots(tmp_path: Path) -> None:
    policy, safe = _policy(tmp_path)
    (safe / "note.txt").write_text("hello", encoding="utf-8")
    ctx = Ctx(policy)
    listed = mobile_ops.list_files(ctx, {"path": str(safe)})
    assert listed["ok"] and any(e["name"] == "note.txt" for e in listed["entries"])
    assert mobile_ops.read_file(ctx, {"path": str(safe / "note.txt")})["text"] == "hello"
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    assert code_of(lambda: mobile_ops.read_file(ctx, {"path": str(outside)})) == "outside_safe"


def test_upload_refuses_outside_and_overwrite(tmp_path: Path) -> None:
    policy, safe = _policy(tmp_path)
    ctx = Ctx(policy)
    data = base64.b64encode(b"data").decode()
    saved = mobile_ops.put_file(ctx, {"path": str(safe), "name": "up.bin", "data": data})
    assert saved["ok"] and (safe / "up.bin").read_bytes() == b"data"
    assert code_of(lambda: mobile_ops.put_file(ctx, {"path": str(safe), "name": "up.bin", "data": data})) == "exists"
    assert code_of(lambda: mobile_ops.put_file(ctx, {"path": str(safe), "name": "../escape", "data": data})) \
        in ("outside_safe", "malformed", "invalid_path")


class LockedCtx(Ctx):
    pass


def test_screen_refused_when_locked(tmp_path: Path, monkeypatch) -> None:
    policy, _ = _policy(tmp_path)

    class S:
        screen_capture_enabled = True

    monkeypatch.setattr(pc_state, "session", lambda: pc_state.Session(True, False, False, "test"))
    assert code_of(lambda: mobile_ops.screen_frame(Ctx(policy, S()), {})) == "locked"


# ------------------------------------------------------------------ command mapping


def test_new_control_commands_map_to_fixed_tools() -> None:
    assert mobile.tool_call_for("brightness", {"action": "set", "level": 40}) == \
        ("brightness_control", {"action": "set", "level": 40})
    assert mobile.tool_call_for("close_app", {"name": "chrome"}) == ("close_application", {"name": "chrome"})
    assert mobile.tool_call_for("window", {"action": "minimize_all"}) == ("window_control", {"action": "minimize_all"})
    assert mobile.browser_call_for({"action": "open", "url": "https://x.test"}) == \
        ("browser_open", {"url": "https://x.test"})
    assert mobile.browser_call_for({"action": "click", "target": 3}) == ("browser_click", {"element": 3})
    assert code_of(lambda: mobile.tool_call_for("brightness", {"action": "set", "level": 900})) == "malformed"


def test_apk_info_when_missing() -> None:
    from app.remote import apk

    info = apk.info()
    assert "available" in info and info["version"] == apk.MOBILE_APP_VERSION


# ------------------------------------------------------------------ screen frame (regression: shot.source)


def test_screen_frame_uses_the_real_capture_field(tmp_path: Path, monkeypatch) -> None:
    """Regression: screen_frame once read a non-existent 'shot.what' and crashed with AttributeError."""
    from app.vision import capture as cap

    policy, _ = _policy(tmp_path)

    class S:
        screen_capture_enabled = True

    class FakeImage:
        width, height = 800, 600

    monkeypatch.setattr(pc_state, "session", lambda: pc_state.Session(False, False, True, "test"))
    monkeypatch.setattr(cap, "capture", lambda ctx, target="window": cap.Capture(FakeImage(), "the whole screen"))
    monkeypatch.setattr(cap, "png_bytes", lambda image, max_side=None: b"\x89PNG-bytes")

    import contextlib

    monkeypatch.setattr(cap, "capture_indicator", lambda ctx: contextlib.nullcontext())
    result = mobile_ops.screen_frame(Ctx(policy, S()), {})
    assert result["ok"] and result["what"] == "the whole screen" and result["size"] == [800, 600]
    assert result["image"]  # base64 of the png bytes


def test_wake_config_is_a_read_command() -> None:
    assert mobile.COMMANDS["wake_config"] == ("read", None)
    # read-only: signed with the base key, not the biometric one
    assert "wake_config" not in mobile.STEP_UP_COMMANDS
