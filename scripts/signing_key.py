"""APK status and the signing-key decision helper. Read-only except `new-key --yes` (makes a NEW key; never touches the old one).

  python scripts/signing_key.py status    # APK vs source, certificate fingerprint, is the key in git history
  python scripts/signing_key.py plan      # the options and what each breaks
  python scripts/signing_key.py new-key --yes
"""

from __future__ import annotations

import re
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.remote import apk_check  # noqa: E402


def certificate_sha256(apk: Path) -> str:
    try:
        out = subprocess.run(["keytool", "-printcert", "-jarfile", str(apk)], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"SHA256:\s*([0-9A-F:]{95})", out)
    return m.group(1) if m else ""


def history_exposure(root: Path) -> dict[str, list[str]]:
    """Signing-related files that are tracked now, and commits (any branch) that ever contained them."""
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    tracked = [p for p in git("ls-files", apk_check.SIGNING_DIR).splitlines() if p]
    commits = [c for c in git("log", "--all", "--format=%h %ad %s", "--date=short", "--", apk_check.SIGNING_DIR, "*.jks", "*.keystore").splitlines() if c]
    return {"tracked_now": tracked, "in_history": commits}


def status() -> int:
    report = apk_check.check(ROOT)
    report.certificate_sha256 = certificate_sha256(ROOT / "app" / "remote" / "assets" / "JarvisMobile.apk")
    print(report.describe())
    exposure = history_exposure(ROOT)
    print(f"\nSigning files tracked by git now: {', '.join(exposure['tracked_now']) or 'none'}")
    print("Commits that ever contained signing files:\n  " + ("\n  ".join(exposure["in_history"]) or "none"))
    if exposure["in_history"]:
        print("\nTreat the key as exposed if the repository is or was public. Run: python scripts/signing_key.py plan")
    return 0


def new_key() -> int:
    target = ROOT / "android" / "signing" / "new"
    keystore = target / "jarvis-mobile-2.jks"
    if keystore.exists():
        print(f"{keystore} already exists; not overwriting it.")
        return 1
    target.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)
    (target / "password.txt").write_text(password, encoding="utf-8")
    subprocess.run(["keytool", "-genkeypair", "-keystore", str(keystore), "-storepass", password, "-keypass", password,
                    "-alias", "jarvismobile", "-keyalg", "RSA", "-keysize", "3072", "-validity", "9000",
                    "-dname", "CN=Jarvis Mobile, O=Personal"], check=True)
    print(f"Made {keystore} (password in {target / 'password.txt'}; both are git-ignored - back them up somewhere private).")
    print("Point packaging\\build_android.ps1 at it, build, then uninstall the old app on the phone, install the new APK and pair again.")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        return status()
    if cmd == "plan":
        print(apk_check.PLAN)
        return 0
    if cmd == "new-key":
        if "--yes" not in argv:
            print("This makes a NEW signing key (the old one is untouched). Add --yes to proceed.")
            return 1
        return new_key()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
