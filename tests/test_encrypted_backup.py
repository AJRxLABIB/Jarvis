"""Encrypted backups: round trip, wrong password, tampering, no overwrite."""

from __future__ import annotations

import sqlite3

import pytest

from app.memory import encrypted_backup as eb
from app.memory.database import StorageError


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    original = eb._key
    monkeypatch.setattr(eb, "_key", lambda p, s, n=2 ** 10: original(p, s, n))


def _db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('secret note')")
    conn.commit()
    conn.close()


def test_round_trip_and_wrong_password(tmp_path):
    _db(tmp_path / "a.db")
    usb = tmp_path / "usb"
    usb.mkdir()
    file = eb.save(tmp_path / "a.db", usb, "correct horse")
    assert file.suffix == ".jbk" and b"secret note" not in file.read_bytes()
    with pytest.raises(StorageError):
        eb.open_to(file, tmp_path / "bad.db", "wrong password")
    assert not (tmp_path / "bad.db").exists()
    out = eb.open_to(file, tmp_path / "back.db", "correct horse")
    assert sqlite3.connect(out).execute("SELECT x FROM t").fetchone() == ("secret note",)
    with pytest.raises(StorageError):
        eb.open_to(file, out, "correct horse")                     # never overwrites


def test_tampering_short_password_and_missing_drive(tmp_path):
    _db(tmp_path / "a.db")
    with pytest.raises(StorageError):
        eb.save(tmp_path / "a.db", tmp_path / "nope", "correct horse")
    with pytest.raises(StorageError):
        eb.seal(b"x", "short")
    blob = bytearray(eb.seal(b"data", "correct horse"))
    blob[-1] ^= 1
    with pytest.raises(StorageError):
        eb.unseal(bytes(blob), "correct horse")
    with pytest.raises(StorageError):
        eb.unseal(b"not a backup at all, definitely", "correct horse")
