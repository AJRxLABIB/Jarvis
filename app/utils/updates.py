"""Update check (UPDATE_MANIFEST_URL, off when empty).

The manifest is a small JSON file you publish with each release, e.g. on GitHub:
    {"version": "2.1.0", "url": "https://.../JarvisSetup-2.1.0.exe",
     "sha256": "<hex>", "notes": "What's new", "signed": true}
Jarvis compares the version, says when an update is available, and - only after you agree -
downloads the installer over HTTPS, checks its SHA-256 and its Windows code signature
(Authenticode), and starts it. A broken signature is always refused; an unsigned installer is
refused when the manifest says "signed": true or UPDATE_REQUIRE_SIGNATURE is on.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app import __version__

TRANSPORT: httpx.BaseTransport | None = None  # tests replace this


MAX_MANIFEST_BYTES = 256 * 1024
MAX_INSTALLER_BYTES = 1024 * 1024 * 1024


class UpdateError(Exception):
    pass


def _require_https(response: httpx.Response) -> None:
    """A redirect must not drop the connection to plain http (the manifest and installer travel over https only)."""
    for hop in [*response.history, response]:
        if hop.url.scheme != "https":
            raise UpdateError("The update server redirected to an unencrypted address; the update was refused.")


def safe_installer_name(info: "UpdateInfo") -> str:
    """A plain file name for the installer: letters, digits, dot, dash, underscore; .exe or .msi only."""
    raw = Path(info.url.split("?")[0].split("#")[0]).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", raw).lstrip(".")
    if not cleaned.lower().endswith((".exe", ".msi")):
        cleaned = f"JarvisSetup-{re.sub(r'[^0-9A-Za-z.]', '', info.version) or 'update'}.exe"
    return cleaned[:100]


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    sha256: str
    notes: str
    signed: bool = False  # the release promises a code signature: an unsigned download is refused


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:4]) or (0,)


def check(manifest_url: str, current: str = __version__) -> UpdateInfo | None:
    """The newer version described by the manifest, or None if this one is current."""
    if not manifest_url.lower().startswith("https://"):
        raise UpdateError("The update manifest must be an https:// URL.")
    try:
        with httpx.Client(transport=TRANSPORT, timeout=10.0, follow_redirects=True, max_redirects=5,
                          trust_env=False) as client:
            response = client.get(manifest_url)
            response.raise_for_status()
            _require_https(response)
            if len(response.content) > MAX_MANIFEST_BYTES:
                raise UpdateError("The update manifest is unreasonably large and was ignored.")
            data = response.json()
    except UpdateError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise UpdateError(f"Could not check for updates: {exc}") from exc
    try:
        info = UpdateInfo(str(data["version"]), str(data["url"]), str(data["sha256"]).lower(), str(data.get("notes", "")),
                          bool(data.get("signed", False)))
    except (KeyError, TypeError) as exc:
        raise UpdateError("The update manifest is incomplete (version, url and sha256 are required).") from exc
    if not info.url.lower().startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", info.sha256):
        raise UpdateError("The update manifest has an invalid download URL or checksum.")
    return info if parse_version(info.version) > parse_version(current) else None


def download(info: UpdateInfo, folder: Path, require_signature: bool = False) -> Path:
    """Download the installer and verify its SHA-256 and code signature (a failure deletes it)."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / safe_installer_name(info)
    digest = hashlib.sha256()
    written = 0
    try:
        with httpx.Client(transport=TRANSPORT, timeout=60.0, follow_redirects=True, max_redirects=5,
                          trust_env=False) as client:
            with client.stream("GET", info.url) as response:
                response.raise_for_status()
                _require_https(response)
                with target.open("wb") as out:
                    for chunk in response.iter_bytes():
                        written += len(chunk)
                        if written > MAX_INSTALLER_BYTES:
                            raise UpdateError("The download is larger than any Jarvis installer should be; stopped.")
                        digest.update(chunk)
                        out.write(chunk)
    except UpdateError:
        target.unlink(missing_ok=True)
        raise
    except httpx.HTTPError as exc:
        target.unlink(missing_ok=True)   # never leave a half-downloaded installer behind
        raise UpdateError(f"The download failed: {exc}") from exc
    if digest.hexdigest() != info.sha256:
        target.unlink(missing_ok=True)
        raise UpdateError("The downloaded update failed its checksum and was deleted.")
    status = signature_status(target)
    if status == "invalid" or (status != "valid" and (info.signed or require_signature)):
        target.unlink(missing_ok=True)
        why = {"invalid": "its code signature is broken or not trusted", "unsigned": "it is not signed",
               "unknown": "its signature could not be checked"}[status]
        raise UpdateError(f"The downloaded update was refused and deleted: {why}.")
    return target


SIGNATURE_CHECK = None  # tests replace this: path -> "valid" | "unsigned" | "invalid" | "unknown"


def signature_status(path: Path) -> str:
    """Authenticode check with WinVerifyTrust (no program is started): valid, unsigned, invalid or unknown."""
    if SIGNATURE_CHECK is not None:
        return SIGNATURE_CHECK(path)
    import sys

    if sys.platform != "win32":
        return "unknown"
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR), ("hFile", wintypes.HANDLE),
                    ("pgKnownSubject", ctypes.POINTER(GUID))]

    class TRUST_DATA(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(FILE_INFO)), ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
                    ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    action = GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    file_info = FILE_INFO(ctypes.sizeof(FILE_INFO), str(path), None, None)
    data = TRUST_DATA()
    data.cbStruct = ctypes.sizeof(TRUST_DATA)
    data.dwUIChoice = 2  # WTD_UI_NONE
    data.fdwRevocationChecks = 0
    data.dwUnionChoice = 1  # WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwProvFlags = 0x10  # WTD_CACHE_ONLY_URL_RETRIEVAL: no network during the check
    try:
        result = ctypes.windll.wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data)) & 0xFFFFFFFF
    except Exception:
        return "unknown"
    if result == 0:
        return "valid"
    if result in (0x800B0100, 0x800B0003):  # TRUST_E_NOSIGNATURE, TRUST_E_SUBJECT_FORM_UNKNOWN
        return "unsigned"
    return "invalid"
