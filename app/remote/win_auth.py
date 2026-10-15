"""What Windows on this PC supports for signing in - read from Windows, never assumed.

JARVIS Mobile shows this so you know which SUPPORTED way exists to get into a locked PC. Jarvis itself never
signs in, never unlocks, never sees, stores or types a Windows password or PIN: Windows stays the only thing
that authenticates a Windows user.

Checks (no admin needed, fixed API calls only - nothing here runs text from a phone or a model):

* edition / version / build             - registry (HKLM\\...\\Windows NT\\CurrentVersion)
* Remote Desktop host: edition allows it - Home ("Core") editions can't host Remote Desktop
* Remote Desktop switched on, port, NLA - registry (Terminal Server)
* Windows Hello hardware                 - WinBio: fingerprint / face sensors present
* Windows Hello PIN for this user        - registry key Windows writes when a PIN is set (unknown if unreadable)
* TPM                                    - TPM Base Services (Tbsi_GetDeviceInfo)
* Microsoft account vs local account     - NetUserGetInfo level 24 (internet identity flag)
* Microsoft Entra ID join                - NetGetAadJoinInformation

Anything that can't be read is reported as "unknown", never guessed.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

_CACHE: tuple[float, "AuthCapabilities"] | None = None
CACHE_SECONDS = 600
HOME_EDITIONS = ("core", "home")  # EditionID "Core", "CoreSingleLanguage", "CoreCountrySpecific", "CoreN"


@dataclass
class AuthCapabilities:
    windows: str = "unknown"          # "Windows 11 Pro 23H2 (build 22631)"
    edition: str = "unknown"          # EditionID, e.g. Professional / Core
    rdp_host_supported: bool | None = None
    rdp_enabled: bool | None = None
    rdp_port: int | None = None
    rdp_nla: bool | None = None
    fingerprint_sensor: bool | None = None
    face_sensor: bool | None = None
    hello_pin: bool | None = None
    tpm: str = "unknown"              # "2.0", "1.2", "none", "unknown"
    account: str = "unknown"          # "microsoft", "local", "unknown"
    entra: str = "unknown"            # "joined", "registered", "none", "unknown"
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ what that means for the phone

    @property
    def remote_desktop(self) -> str:
        """available / disabled / unsupported_edition / unknown"""
        if self.rdp_host_supported is False:
            return "unsupported_edition"
        if self.rdp_enabled is True and self.rdp_host_supported:
            return "available"
        if self.rdp_enabled is False:
            return "disabled"
        return "unknown"

    def sign_in_options(self) -> list[dict[str, Any]]:
        """The supported ways to get a signed-in Windows session when the PC is locked. JARVIS does none of them
        itself - it only tells the phone which exist."""
        options: list[dict[str, Any]] = []
        rd = self.remote_desktop
        options.append({"method": "remote_desktop", "available": rd == "available",
                        "detail": {"available": f"Remote Desktop is on (port {self.rdp_port or 3389}). Sign in with "
                                                "your Windows account in Microsoft's Remote Desktop / Windows App: "
                                                "Windows checks the password, not Jarvis.",
                                   "disabled": "This edition can host Remote Desktop, but it's switched off: "
                                               "Settings > System > Remote Desktop.",
                                   "unsupported_edition": f"Remote Desktop is unavailable on this Windows edition "
                                                          f"({self.edition}); it needs Pro, Enterprise or Education.",
                                   "unknown": "Couldn't read whether Remote Desktop is on."}[rd]})
        local = [m for m, on in (("PIN", self.hello_pin), ("fingerprint", self.fingerprint_sensor),
                                 ("face", self.face_sensor)) if on]
        options.append({"method": "local_sign_in", "available": True,
                        "detail": "At the PC: " + ("Windows Hello (" + ", ".join(local) + ") or password."
                                                   if local else "your Windows password or PIN.")})
        return options

    def remote_unlock(self) -> dict[str, Any]:
        """Can the phone unlock the LOCAL (console) session? No supported mechanism exists for that: say so."""
        return {"supported": False,
                "detail": "Windows has no supported way for another device to unlock this PC's own screen. "
                          "Remote Desktop signs you into your session remotely (the PC's own screen stays locked); "
                          "otherwise sign in at the PC. Jarvis has not tried to bypass the lock screen."}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["remote_desktop"] = self.remote_desktop
        data["sign_in_options"] = self.sign_in_options()
        data["remote_unlock"] = self.remote_unlock()
        return data

    def describe(self) -> str:
        yes = {True: "available", False: "not available", None: "unknown"}
        return (f"{self.windows}. Remote Desktop: {self.remote_desktop.replace('_', ' ')}. "
                f"Windows Hello: PIN {yes[self.hello_pin]}, fingerprint sensor {yes[self.fingerprint_sensor]}, "
                f"face sensor {yes[self.face_sensor]}. TPM: {self.tpm}. Account: {self.account}. "
                f"Microsoft Entra: {self.entra}.")


# ------------------------------------------------------------------ Windows readers (replaced in tests)


class WindowsProbe:
    """Each method returns a value or raises; detect() turns failures into "unknown"."""

    def registry(self, hive: str, path: str, name: str) -> Any:
        import winreg

        root = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}[hive]
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as key:
            return winreg.QueryValueEx(key, name)[0]

    def registry_key_exists(self, hive: str, path: str) -> bool:
        import winreg

        root = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}[hive]
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)):
                return True
        except FileNotFoundError:
            return False

    def user_sid(self) -> str:
        import win32api
        import win32security

        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        return win32security.ConvertSidToStringSid(sid)

    def biometric_units(self, kind: int) -> int:
        winbio = ctypes.WinDLL("winbio.dll")
        schemas, count = ctypes.c_void_p(), ctypes.c_size_t()
        hr = winbio.WinBioEnumBiometricUnits(ctypes.c_uint32(kind), ctypes.byref(schemas), ctypes.byref(count))
        if schemas:
            winbio.WinBioFree(schemas)
        if hr != 0:
            raise OSError(hr)
        return int(count.value)

    def tpm_version(self) -> int:
        class TPM_DEVICE_INFO(ctypes.Structure):
            _fields_ = [("structVersion", ctypes.c_uint32), ("tpmVersion", ctypes.c_uint32),
                        ("tpmInterfaceType", ctypes.c_uint32), ("tpmImpRevision", ctypes.c_uint32)]

        info = TPM_DEVICE_INFO()
        result = ctypes.WinDLL("tbs.dll").Tbsi_GetDeviceInfo(ctypes.sizeof(info), ctypes.byref(info))
        if result == 0x8028400F:  # TBS_E_TPM_NOT_FOUND
            return 0
        if result != 0:
            raise OSError(result)
        return int(info.tpmVersion)

    def microsoft_account(self) -> bool:
        class USER_INFO_24(ctypes.Structure):
            _fields_ = [("internet_identity", ctypes.c_int), ("flags", ctypes.c_uint32),
                        ("provider", ctypes.c_wchar_p), ("principal", ctypes.c_wchar_p), ("sid", ctypes.c_void_p)]

        netapi = ctypes.WinDLL("netapi32.dll")
        buffer = ctypes.c_void_p()
        status = netapi.NetUserGetInfo(None, ctypes.c_wchar_p(os.environ.get("USERNAME", "")), 24, ctypes.byref(buffer))
        if status != 0:
            raise OSError(status)
        try:
            return bool(ctypes.cast(buffer, ctypes.POINTER(USER_INFO_24)).contents.internet_identity)
        finally:
            netapi.NetApiBufferFree(buffer)

    def entra_join(self) -> int:
        """0 none, 1 joined, 2 registered (work account)."""
        netapi = ctypes.WinDLL("netapi32.dll")
        info = ctypes.c_void_p()
        hr = netapi.NetGetAadJoinInformation(None, ctypes.byref(info))
        if hr != 0:
            raise OSError(hr)
        if not info:
            return 0
        try:
            return int(ctypes.cast(info, ctypes.POINTER(ctypes.c_int)).contents.value)  # first field: joinType
        finally:
            netapi.NetFreeAadJoinInformation(info)


_NT = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
_TS = r"SYSTEM\CurrentControlSet\Control\Terminal Server"
_RDP_TCP = _TS + r"\WinStations\RDP-Tcp"
_NGC_PIN = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\LogonUI\NgcPin\Credentials"
WINBIO_TYPE_FACIAL_FEATURES, WINBIO_TYPE_FINGERPRINT = 0x02, 0x08


def _try(function, *args):
    try:
        return function(*args), None
    except Exception as exc:  # noqa: BLE001 - every failure is reported as "unknown"
        return None, exc.__class__.__name__


def detect(probe: Any = None) -> AuthCapabilities:
    """Read this PC's sign-in capabilities (runs only on Windows; elsewhere everything is unknown)."""
    caps = AuthCapabilities()
    if probe is None:
        if sys.platform != "win32":
            caps.notes.append("not Windows")
            return caps
        probe = WindowsProbe()
    reg = probe.registry
    name, _ = _try(reg, "HKLM", _NT, "ProductName")
    display, _ = _try(reg, "HKLM", _NT, "DisplayVersion")
    build, _ = _try(reg, "HKLM", _NT, "CurrentBuild")
    edition, _ = _try(reg, "HKLM", _NT, "EditionID")
    if name:
        try:
            if build and int(build) >= 22000:  # the registry still says "Windows 10" on Windows 11
                name = str(name).replace("Windows 10", "Windows 11")
        except ValueError:
            pass
        caps.windows = " ".join(str(p) for p in (name, display) if p) + (f" (build {build})" if build else "")
    if edition:
        caps.edition = str(edition)
        caps.rdp_host_supported = not str(edition).lower().startswith(HOME_EDITIONS)
    deny, error = _try(reg, "HKLM", _TS, "fDenyTSConnections")
    if deny is not None:
        caps.rdp_enabled = int(deny) == 0
    elif error:
        caps.notes.append(f"Remote Desktop setting unreadable ({error})")
    port, _ = _try(reg, "HKLM", _RDP_TCP, "PortNumber")
    caps.rdp_port = int(port) if port is not None else (3389 if caps.rdp_enabled else None)
    nla, _ = _try(reg, "HKLM", _RDP_TCP, "UserAuthentication")
    caps.rdp_nla = bool(int(nla)) if nla is not None else None
    fingers, _ = _try(probe.biometric_units, WINBIO_TYPE_FINGERPRINT)
    faces, _ = _try(probe.biometric_units, WINBIO_TYPE_FACIAL_FEATURES)
    caps.fingerprint_sensor = fingers > 0 if fingers is not None else None
    caps.face_sensor = faces > 0 if faces is not None else None
    sid, _ = _try(probe.user_sid)
    if sid:
        pin, _ = _try(probe.registry_key_exists, "HKLM", _NGC_PIN + "\\" + sid)
        caps.hello_pin = pin if pin else None  # a missing/unreadable key isn't proof there is no PIN
    tpm, _ = _try(probe.tpm_version)
    caps.tpm = {None: "unknown", 0: "none", 1: "1.2", 2: "2.0"}.get(tpm, "unknown")
    msa, _ = _try(probe.microsoft_account)
    caps.account = {None: "unknown", True: "microsoft", False: "local"}[msa]
    join, _ = _try(probe.entra_join)
    caps.entra = {None: "unknown", 0: "none", 1: "joined", 2: "registered"}.get(join, "unknown")
    return caps


def cached(probe: Any = None) -> AuthCapabilities:
    global _CACHE
    now = time.monotonic()
    if probe is None and _CACHE is not None and now - _CACHE[0] < CACHE_SECONDS:
        return _CACHE[1]
    caps = detect(probe)
    if probe is None:
        _CACHE = (now, caps)
    return caps
