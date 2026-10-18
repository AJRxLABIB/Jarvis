"""Tamper-evident audit trail: every entry carries a hash that covers the entry before it.

Editing or removing an entry in the middle, or cutting off the newest ones, breaks the chain and
``verify`` says where. Removing the OLDEST entries through the confirmed delete action is allowed and
keeps the rest valid. This detects accidental damage and simple tampering; someone who can rewrite the
whole database and recompute every hash can still forge a chain, so it is evidence, not a vault.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

GENESIS = "0" * 64
_FIELDS = ("ts", "event", "tool", "permission_level", "decision", "details", "run_id")


def entry_hash(prev_hash: str, values: tuple) -> str:
    text = "\x1f".join("" if v is None else str(v) for v in values)
    return hashlib.sha256((prev_hash + "\x1e" + text).encode("utf-8")).hexdigest()


def seal(conn: sqlite3.Connection, row_id: int) -> None:
    """Chain the just-inserted row ``row_id`` (call inside the same transaction as the insert)."""
    head = conn.execute("SELECT last_hash FROM audit_head WHERE id = 1").fetchone()
    prev = head["last_hash"] if head else GENESIS
    row = conn.execute(f"SELECT {', '.join(_FIELDS)} FROM audit_log WHERE id = ?", (row_id,)).fetchone()
    digest = entry_hash(prev, tuple(row[f] for f in _FIELDS))
    conn.execute("UPDATE audit_log SET prev_hash = ?, entry_hash = ? WHERE id = ?", (prev, digest, row_id))
    conn.execute("INSERT INTO audit_head (id, last_id, last_hash) VALUES (1, ?, ?) "
                 "ON CONFLICT(id) DO UPDATE SET last_id = excluded.last_id, last_hash = excluded.last_hash",
                 (row_id, digest))


def reset_if_empty(conn: sqlite3.Connection) -> None:
    """After a confirmed delete: with no entries left, the chain starts over."""
    if conn.execute("SELECT 1 FROM audit_log LIMIT 1").fetchone() is None:
        conn.execute("DELETE FROM audit_head")


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    checked: int
    unchained: int          # entries written before the chain existed
    problems: tuple[str, ...]

    def describe(self) -> str:
        if self.ok:
            extra = f" ({self.unchained} older entries pre-date the chain and are not covered)" if self.unchained else ""
            return f"Activity log intact: {self.checked} chained entries verified{extra}."
        return (f"Activity log FAILED verification after checking {self.checked} entries: "
                + "; ".join(self.problems[:5]))


def verify(database) -> ChainReport:
    problems: list[str] = []
    checked = unchained = 0
    previous: str | None = None
    last_id = None
    with database.connect() as conn:
        rows = conn.execute(f"SELECT id, prev_hash, entry_hash, {', '.join(_FIELDS)} FROM audit_log ORDER BY id").fetchall()
        head = conn.execute("SELECT last_id, last_hash FROM audit_head WHERE id = 1").fetchone()
    for row in rows:
        if row["entry_hash"] is None:
            unchained += 1
            if previous is not None:
                problems.append(f"entry {row['id']} has no hash inside the chain")
            continue
        expected = entry_hash(row["prev_hash"], tuple(row[f] for f in _FIELDS))
        if expected != row["entry_hash"]:
            problems.append(f"entry {row['id']} was changed")
        if previous is not None and row["prev_hash"] != previous:
            problems.append(f"entries before {row['id']} were removed or altered")
        previous = row["entry_hash"]
        last_id = row["id"]
        checked += 1
    if head is not None:
        if previous is None:
            problems.append("all chained entries are missing")
        elif head["last_hash"] != previous or head["last_id"] != last_id:
            problems.append("the newest entries were removed")
    elif previous is not None:
        problems.append("the chain head is missing")
    return ChainReport(not problems, checked, unchained, tuple(problems))
