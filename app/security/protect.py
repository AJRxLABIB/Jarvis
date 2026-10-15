"""Encryption for your secrets on this PC with Windows DPAPI.

DPAPI ties the data to your Windows account: another user of the PC, or someone who copies the
files to another computer, can't read them. Used for secret settings (HOME_ASSISTANT_TOKEN is
written to .env as "dpapi:..."), contacts, your voice print, your wake-word recordings and the
phone-remote key. Files written before (unencrypted) are still read, and are encrypted the next
time they are saved. On other systems (tests) data is stored as before.
"""

from __future__ import annotations

import base64
import ctypes
import sys
from pathlib import Path

PREFIX = "dpapi:"
MAGIC = b"JARVIS-DPAPI1\n"
_ENTROPY = b"JarvisAssistant"  # an extra app-specific value; not a secret by itself

BACKEND = None  # tests replace this with an object that has protect(bytes) / unprotect(bytes)


class ProtectError(Exception):
    pass


class _Dpapi:
    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _call(self, name: str, data: bytes) -> bytes:
        crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
        buffer = ctypes.create_string_buffer(data, len(data))
        source = self._Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        salt_buffer = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
        salt = self._Blob(len(_ENTROPY), ctypes.cast(salt_buffer, ctypes.POINTER(ctypes.c_char)))
        out = self._Blob()
        CRYPTPROTECT_UI_FORBIDDEN = 0x1
        if name == "protect":
            ok = crypt32.CryptProtectData(ctypes.byref(source), None, ctypes.byref(salt), None, None,
                                          CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
        else:
            ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(salt), None, None,
                                            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
        if not ok:
            raise ProtectError("Windows could not " + ("encrypt" if name == "protect" else "decrypt") + " the data "
                               "(was it saved by another Windows user?)")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)

    def protect(self, data: bytes) -> bytes:
        return self._call("protect", data)

    def unprotect(self, data: bytes) -> bytes:
        return self._call("unprotect", data)


def _backend():
    if BACKEND is not None:
        return BACKEND
    return _Dpapi() if sys.platform == "win32" else None


def available() -> bool:
    return _backend() is not None


# ------------------------------------------------------------------ text (settings)

def protect_text(value: str) -> str:
    backend = _backend()
    if not value or value.startswith(PREFIX) or backend is None:
        return value
    return PREFIX + base64.b64encode(backend.protect(value.encode("utf-8"))).decode("ascii")


def reveal_text(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value
    backend = _backend()
    if backend is None:
        raise ProtectError("This secret was encrypted on Windows and can only be read there.")
    try:
        raw = base64.b64decode(value[len(PREFIX):], validate=True)
    except ValueError as exc:
        raise ProtectError("The encrypted secret is damaged.") from exc
    return backend.unprotect(raw).decode("utf-8")


# ------------------------------------------------------------------ files

def write_bytes(path: Path, data: bytes) -> None:
    backend = _backend()
    payload = MAGIC + backend.protect(data) if backend is not None else data
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def read_bytes(path: Path) -> bytes:
    data = Path(path).read_bytes()
    if not data.startswith(MAGIC):
        return data  # written before encryption was added
    backend = _backend()
    if backend is None:
        raise ProtectError("This file was encrypted on Windows and can only be read there.")
    return backend.unprotect(data[len(MAGIC):])


def is_protected(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except OSError:
        return False
