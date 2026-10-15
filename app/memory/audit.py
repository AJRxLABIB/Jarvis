"""Audit trail of tool calls and permission decisions (redacted, local only)."""

from __future__ import annotations

import json
from typing import Any

from app.memory.database import Database, utc_now_iso
from app.utils.logging_setup import get_logger
from app.utils.redaction import redact_value

_log = get_logger("audit")
_MAX_DETAILS = 4000


class AuditLog:
    def __init__(self, database: Database) -> None:
        self._db = database

    def record(
        self, event: str, *, tool: str | None = None, level: int | None = None,
        decision: str | None = None, details: dict[str, Any] | None = None,
    ) -> None:
        from app.core.runs import current

        payload = json.dumps(redact_value(details or {}), ensure_ascii=False, default=str)[:_MAX_DETAILS]
        run = current()
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO audit_log (ts, event, tool, permission_level, decision, details, run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (utc_now_iso(), event, tool, level, decision, payload, run.id if run is not None else None),
                )
        except Exception:  # auditing must never break the action itself
            _log.warning("could not write audit entry", exc_info=True)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
