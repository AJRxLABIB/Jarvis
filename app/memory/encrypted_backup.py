"""Encrypted copy of the database for a USB stick or another drive (local only, no cloud, no extra software).

The snapshot is made with SQLite's backup API (safe while Jarvis runs), then encrypted with a key derived from YOUR
password (scrypt + AES-256-GCM). The password is never stored: lose it and the file cannot be opened. Opening checks
the result as a real database before anything is written.

    python scripts/encrypted_backup.py save  E:\\               # asks for the password
    python scripts/encrypted_backup.py open  E:\\jarvis-....jbk  restored.db
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from app.memory.backup import _check
from app.memory.database import StorageError

MAGIC = b"JBK1"
SALT, NONCE = 16, 12
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
MIN_PASSWORD = 8


def _key(password: str, salt: bytes, n: int = SCRYPT_N) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=n, r=SCRYPT_R, p=SCRYPT_P).derive(password.encode("utf-8"))


def seal(data: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(password) < MIN_PASSWORD:
        raise StorageError(f"Use a password of at least {MIN_PASSWORD} characters.")
    salt, nonce = os.urandom(SALT), os.urandom(NONCE)
    return MAGIC + salt + nonce + AESGCM(_key(password, salt)).encrypt(nonce, data, MAGIC)


def unseal(blob: bytes, password: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + SALT + NONCE + 16:
        raise StorageError("That is not a Jarvis encrypted backup.")
    salt = blob[4:4 + SALT]
    nonce = blob[4 + SALT:4 + SALT + NONCE]
    try:
        return AESGCM(_key(password, salt)).decrypt(nonce, blob[4 + SALT + NONCE:], MAGIC)
    except InvalidTag:
        raise StorageError("Wrong password, or the file was changed or damaged.") from None


def save(db_path: Path, destination: Path, password: str, *, now: datetime | None = None) -> Path:
    """Snapshot ``db_path`` and write an encrypted ``jarvis-<time>.jbk`` into the ``destination`` folder."""
    if not destination.is_dir():
        raise StorageError(f"{destination} is not a folder (is the drive plugged in?).")
    with tempfile.TemporaryDirectory(prefix="jarvis-enc-") as scratch:
        snap = Path(scratch) / "snapshot.db"
        source = sqlite3.connect(str(db_path))
        try:
            dest = sqlite3.connect(str(snap))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        result, _version = _check(snap)
        if result != "ok":
            raise StorageError(f"The snapshot failed its integrity check ({result}); nothing was written.")
        blob = seal(snap.read_bytes(), password)
    target = destination / f"jarvis-{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}.jbk"
    staged = target.with_suffix(".part")
    staged.write_bytes(blob)
    os.replace(staged, target)
    return target


def open_to(backup: Path, output: Path, password: str) -> Path:
    """Decrypt ``backup`` to ``output`` (a .db file). Never overwrites; the result is checked as a database."""
    if output.exists():
        raise StorageError(f"{output} already exists - I won't overwrite it.")
    data = unseal(backup.read_bytes(), password)
    staged = output.with_suffix(".part")
    staged.write_bytes(data)
    try:
        result, _version = _check(staged)
    except StorageError:
        staged.unlink(missing_ok=True)
        raise
    if result != "ok":
        staged.unlink(missing_ok=True)
        raise StorageError(f"The decrypted file failed its integrity check ({result}).")
    os.replace(staged, output)
    return output
