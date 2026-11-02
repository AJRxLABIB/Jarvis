"""Check the bundled Android app (JarvisMobile.apk) against its source, and help with the signing-key decision.

Read-only facts (no process is started here; the certificate and git look-ups live in scripts/signing_key.py): the version string compiled into the APK, the version in the source manifest and in ``MOBILE_APP_VERSION``, whether
any source file is newer than the APK (it would then be out of date), the APK's signing-certificate fingerprint and whether signing files are or were in git (both filled in by the script). It never reads the key's password, never signs or builds anything, and
never rewrites git history. Building the APK needs the Android SDK and your key (packaging/build_android.ps1 on the PC).
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SIGNING_DIR = "android/signing"


@dataclass
class ApkReport:
    exists: bool = False
    apk_version: str = ""
    source_version: str = ""
    declared_version: str = ""
    stale_sources: list[str] = field(default_factory=list)
    certificate_sha256: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def up_to_date(self) -> bool:
        return bool(self.exists and self.apk_version and self.apk_version == self.source_version == self.declared_version
                    and not self.stale_sources)

    def describe(self) -> str:
        if not self.exists:
            return "There is no bundled JarvisMobile.apk."
        rows = [f"APK reports version {self.apk_version or '?'}; source manifest {self.source_version or '?'}; "
                f"MOBILE_APP_VERSION {self.declared_version or '?'}.",
                f"Source files newer than the APK: {', '.join(self.stale_sources[:5]) or 'none'}.",
                f"Signing certificate SHA-256: {self.certificate_sha256 or 'unknown (keytool/JDK not available)'}."]
        rows.append("The APK matches its source." if self.up_to_date else "The APK may be OUT OF DATE or inconsistent - rebuild it on the PC "
                    "(packaging\\build_android.ps1).")
        return "\n".join(rows + self.notes)


def _utf16_versions(data: bytes) -> list[str]:
    text = data.decode("utf-16le", errors="ignore")
    return re.findall(r"(?<![\d.])\d{1,2}\.\d{1,2}\.\d{1,3}(?![\d.])", text)


def apk_version(apk: Path) -> str:
    """The versionName string compiled into the APK's binary manifest."""
    with zipfile.ZipFile(apk) as z:
        found = _utf16_versions(z.read("AndroidManifest.xml"))
    return found[0] if found else ""


def source_version(manifest: Path) -> str:
    m = re.search(r'android:versionName="([^"]+)"', manifest.read_text(encoding="utf-8", errors="ignore"))
    return m.group(1) if m else ""


def check(root: Path) -> ApkReport:
    apk = root / "app" / "remote" / "assets" / "JarvisMobile.apk"
    src = root / "android" / "JarvisMobile"
    r = ApkReport(exists=apk.is_file())
    if not r.exists:
        return r
    try:
        r.apk_version = apk_version(apk)
    except (OSError, KeyError, zipfile.BadZipFile):
        r.notes.append("The APK could not be read as an archive.")
    manifest = src / "AndroidManifest.xml"
    if manifest.is_file():
        r.source_version = source_version(manifest)
    declared = root / "app" / "remote" / "apk.py"
    if declared.is_file():
        m = re.search(r'MOBILE_APP_VERSION\s*=\s*"([^"]+)"', declared.read_text(encoding="utf-8"))
        r.declared_version = m.group(1) if m else ""
    mtime = apk.stat().st_mtime
    r.stale_sources = sorted(str(p.relative_to(root)).replace("\\", "/") for p in src.rglob("*")
                             if p.is_file() and p.stat().st_mtime > mtime + 2)
    return r


PLAN = """The signing key is in git history (the exposure above). What each choice means:

A. Keep the key. Fine ONLY if the GitHub repository has always been private and nobody else has a copy. Nothing breaks. Check on GitHub
   (Settings -> General -> Danger Zone shows Public/Private; forks and clones also hold the history). If unsure, assume exposed.
B. Rotate the key (recommended if the repo is or ever was public). An attacker with the old key could sign an update that Android
   would accept as yours. `python scripts/signing_key.py new-key --yes` makes a new key in android/signing/new/ (git-ignored). Then build
   the APK with it. Android refuses an update signed with a different key, so on the phone: uninstall JARVIS Mobile, install the new APK,
   pair again (the PC's Trust step). Your PC data is not affected.
C. Scrub the history (git filter-repo --path android/signing --invert-paths, then force-push). This rewrites every commit id, everyone must
   re-clone, and it does NOT undo exposure: GitHub caches, forks and earlier clones keep the old objects. Use it together with B, not instead.

I do not choose for you and I never rewrite history or push. Whatever you pick, keep android/signing and *.jks out of git (.gitignore
already lists them) and never commit .env or password files.
"""
