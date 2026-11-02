"""APK vs source consistency and signing-key helper (read-only facts; nothing is built, signed or pushed)."""

import os
import subprocess
import time
import zipfile
from pathlib import Path

import importlib.util

from app.remote import apk_check as A

_spec = importlib.util.spec_from_file_location("signing_key", Path(__file__).resolve().parent.parent / "scripts" / "signing_key.py")
SK = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SK)


def make_root(tmp_path: Path, apk_ver="1.4.0", src_ver="1.4.0", declared="1.4.0") -> Path:
    (tmp_path / "app" / "remote" / "assets").mkdir(parents=True)
    (tmp_path / "android" / "JarvisMobile").mkdir(parents=True)
    with zipfile.ZipFile(tmp_path / "app" / "remote" / "assets" / "JarvisMobile.apk", "w") as z:
        z.writestr("AndroidManifest.xml", b"\x03\x00junk" + f"com.jarvis.mobile {apk_ver} x".encode("utf-16le"))
    (tmp_path / "android" / "JarvisMobile" / "AndroidManifest.xml").write_text(f'<manifest android:versionName="{src_ver}">')
    (tmp_path / "app" / "remote" / "apk.py").write_text(f'MOBILE_APP_VERSION = "{declared}"\n')
    old = time.time() - 100
    os.utime(tmp_path / "app" / "remote" / "assets" / "JarvisMobile.apk", (time.time(), time.time()))
    os.utime(tmp_path / "android" / "JarvisMobile" / "AndroidManifest.xml", (old, old))
    return tmp_path


def test_consistent_apk(tmp_path):
    r = A.check(make_root(tmp_path))
    assert r.apk_version == "1.4.0" and r.up_to_date and "matches its source" in r.describe()


def test_version_mismatch_is_reported(tmp_path):
    r = A.check(make_root(tmp_path, apk_ver="1.3.0"))
    assert not r.up_to_date and "OUT OF DATE" in r.describe()


def test_source_newer_than_apk_means_stale(tmp_path):
    root = make_root(tmp_path)
    f = root / "android" / "JarvisMobile" / "New.java"
    f.write_text("class New {}")
    os.utime(f, (time.time() + 60, time.time() + 60))
    r = A.check(root)
    assert r.stale_sources == ["android/JarvisMobile/New.java"] and not r.up_to_date


def test_missing_apk(tmp_path):
    assert A.check(tmp_path).describe() == "There is no bundled JarvisMobile.apk."


def test_history_exposure_lists_commits_and_never_prints_secrets(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "android" / "signing").mkdir(parents=True)
    (tmp_path / "android" / "signing" / "password.txt").write_text("hunter2-secret")
    git("add", "-A")
    git("commit", "-qm", "oops")
    git("rm", "-q", "-r", "--cached", "android/signing")
    git("commit", "-qm", "untrack")
    exposure = SK.history_exposure(tmp_path)
    assert exposure["tracked_now"] == [] and len(exposure["in_history"]) == 2
    assert "hunter2" not in str(exposure)


def test_plan_does_not_decide_or_rewrite():
    assert "I do not choose for you and I never rewrite history or push" in A.PLAN
    assert "does NOT undo exposure" in A.PLAN


def test_the_script_refuses_to_make_a_key_without_yes(capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("signing_key", Path(__file__).resolve().parent.parent / "scripts" / "signing_key.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["new-key"]) == 1 and "--yes" in capsys.readouterr().out
