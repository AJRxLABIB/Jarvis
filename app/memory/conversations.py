"""Conversation metadata (not message text) stored in SQLite."""

from __future__ import annotations

import uuid

from app.memory.database import Database, utc_now_iso
from app.utils.redaction import redact

_TITLE_LENGTH = 60


class ConversationStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def start(self, model: str | None = None) -> str:
        conversation_id = uuid.uuid4().hex
        now = utc_now_iso()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO conversations (id, model, started_at, last_activity_at) VALUES (?, ?, ?, ?)",
                (conversation_id, model, now, now),
            )
        return conversation_id

    def record_exchange(
        self, conversation_id: str, *, model: str | None, first_user_text: str | None, messages_added: int = 2
    ) -> None:
        title = None
        if first_user_text:
            title = " ".join(redact(first_user_text).split())[:_TITLE_LENGTH] or None
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE conversations SET message_count = message_count + ?, last_activity_at = ?, "
                "model = COALESCE(?, model), title = COALESCE(title, ?) WHERE id = ?",
                (messages_added, utc_now_iso(), model, title, conversation_id),
            )

    def get(self, conversation_id: str) -> dict | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return dict(row) if row else None
