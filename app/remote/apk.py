"""Facts about the built JARVIS Mobile APK, so the PC can offer it honestly (version + SHA-256 to verify).

The APK is built on the PC (packaging\\build_android.ps1) into app/remote/assets/JarvisMobile.apk (the older
JarvisWake.apk name is still accepted). Nothing here builds or downloads anything - it only reports what's on disk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

MOBILE_APP_VERSION = "1.0.0"  # keep in step with android/JarvisMobile/AndroidManifest.xml versionName
_ASSETS = Path(__file__).resolve().parent / "assets"
_NAMES = ("JarvisMobile.apk", "JarvisWake.apk")


def apk_path() -> Path | None:
    for name in _NAMES:
        candidate = _ASSETS / name
        if candidate.is_file():
            return candidate
    return None


def info() -> dict:
    """version, availability, size and SHA-256 of the APK on this PC (available=False when it isn't built yet)."""
    path = apk_path()
    if path is None:
        return {"available": False, "version": MOBILE_APP_VERSION,
                "message": "The JARVIS Mobile app isn't built into this Jarvis yet. On the PC run "
                           "packaging\\build_android.ps1, then rebuild Jarvis."}
    data = path.read_bytes()
    return {"available": True, "version": MOBILE_APP_VERSION, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "filename": path.name}
