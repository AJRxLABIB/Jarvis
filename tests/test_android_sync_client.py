"""The Android sync client (android/JarvisMobile): rules checked on a plain JVM when a JDK is available, and the
list of commands that need the biometric key kept in step with the PC. The Android-only files cannot be compiled here."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.remote import mobile

ANDROID = Path(__file__).resolve().parents[1] / "android" / "JarvisMobile"


def test_the_phone_signs_the_same_commands_with_the_protected_key_as_the_pc_expects() -> None:
    text = (ANDROID / "src/com/jarvis/mobile/Protocol.java").read_text(encoding="utf-8")
    block = text[text.index("STEP_UP"):text.index("));")]
    phone = set(re.findall(r'"([a-z_]+)"', block))
    assert phone == set(mobile.STEP_UP_COMMANDS)


def test_background_job_never_sends_edits() -> None:
    job = (ANDROID / "src/com/jarvis/mobile/SyncJob.java").read_text(encoding="utf-8")
    assert "pullOnly()" in job and ".sync()" not in job and "sync_push" not in job


def test_the_service_is_not_exported_and_needs_the_job_permission() -> None:
    manifest = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
    assert 'android:name=".SyncJob" android:exported="false" android:permission="android.permission.BIND_JOB_SERVICE"' in manifest


@pytest.mark.skipif(shutil.which("javac") is None or shutil.which("java") is None, reason="needs a JDK (javac)")
def test_sync_code_compiles_against_android_stand_ins_and_its_rules_hold() -> None:
    src = ANDROID / "src/com/jarvis/mobile"
    files = [str(src / n) for n in ("Json.java", "SyncClient.java", "AppSync.java", "SyncJob.java", "Store.java", "Pc.java",
                                             "PhoneActions.java", "NotifyForward.java", "ShareActivity.java",
                                             "LiveEvents.java")]
    files += [str(p) for p in sorted((ANDROID / "test/android_api").rglob("*.java"))]
    files += [str(p) for p in sorted((ANDROID / "test/stubs/com").rglob("*.java"))] + [str(ANDROID / "test/SyncClientTest.java")]
    with tempfile.TemporaryDirectory() as out:
        subprocess.run(["javac", "-d", out, *files], check=True, capture_output=True, shell=False)
        done = subprocess.run(["java", "-cp", out, "com.jarvis.mobile.SyncClientTest"], capture_output=True, text=True, shell=False)
    assert done.returncode == 0 and "ALL PASSED" in done.stdout, done.stdout + done.stderr
