"""Small key/value store for settings the user changes at runtime (e.g. selected model)."""

from __future__ import annotations

import json
import re
from typing import Any

from app.memory.database import Database, utc_now_iso

_KEY_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


def _check_key(key: str) -> str:
    if not _KEY_RE.match(key):
        raise ValueError(f"invalid settings key {key!r}")
    return key


class SettingsStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, key: str, default: Any = None) -> Any:
        with self._db.connect() as conn:
            row = conn.execute("SELECT value FROM settings_kv WHERE key = ?", (_check_key(key),)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings_kv (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (_check_key(key), encoded, utc_now_iso()),
            )

    def delete(self, key: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM settings_kv WHERE key = ?", (_check_key(key),))
