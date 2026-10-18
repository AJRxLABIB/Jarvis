"""Selected-data sync for JARVIS Mobile (PC side), and the notification feed it can poll.

Rules, stated once:

* **Opt-in per phone and per dataset, decided on the PC.** Nothing syncs until you switch a dataset on for that phone
  (``mobile_sync`` tool). Datasets are an allowlist: ``todos`` and ``notifications``. Memories, documents, conversations,
  e-mail, settings, secrets and voice recordings are NOT syncable and there is no setting that adds them.
* **Same security as every phone command.** These are ordinary signed mobile commands (app/remote/mobile.py): paired,
  trusted, permission ``sync``, TLS-pinned transport, replay-protected. Writing (``sync_push``) additionally needs the
  phone's biometric-protected key. Revoking a phone stops all of it at once and deletes its sync bookkeeping.
* **The PC is the hub.** Every change to a to-do - by voice, by the window or by a phone - is appended to ``sync_log`` by
  database triggers with a per-record revision number. A phone asks for what changed since the last ``seq`` it saw; the
  first sync (``since = 0``) is a snapshot of the current to-dos.
* **No stale overwrites.** A phone edit carries the revision it was based on (``base_rev``). If the PC changed the record
  since, the edit is NOT applied: the answer is ``conflict`` with the PC's current version, and the phone decides what to
  show. Rule: when both sides changed the same record, the PC's version stands until the phone sends a new edit based on it.
* **Duplicates are harmless.** Every pushed change has an ``op_id``; delivering it twice (retry after a timeout, a flaky
  network) returns the first answer and changes nothing.
* **Deletes are tombstones** (``op: delete``) so a phone that was offline learns about them; a phone that was away longer
  than the log is kept gets ``reset`` and a fresh snapshot.
* **Notifications are generic.** The poll returns a fixed sentence per kind of event ("A reminder is due"), never the
  reminder's text or any content; the phone opens the app and pulls details through a signed request. This is polling:
  it is NOT push, and arrives only while the phone is awake and asks.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

DATASETS = ("todos", "notifications")
MAX_CHANGES = 20            # per push (the signed envelope is limited to 8000 characters)
MAX_PULL = 200
KEEP_LOG = 5000             # change-log rows kept; an older phone gets a fresh snapshot instead
_OP_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TODO_FIELDS = ("text", "due", "done", "minutes")

# event name -> the ONLY text a phone is ever told (no event data is included)
NOTIFICATION_TEXT = {
    "Reminder": "A reminder is due.", "MissionCompleted": "A mission finished.", "MissionPaused": "A mission needs you.",
    "BatteryLow": "The PC battery is low.", "PcProblem": "Jarvis found a problem on the PC.",
    "DownloadCompleted": "A download finished.", "Emergency": "An emergency control was used.",
    "JarvisUpdated": "Jarvis was updated.",
}


class SyncError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SyncStore:
    def __init__(self, database) -> None:
        self._db = database

    # ------------------------------------------------------------------ preferences (set on the PC)

    def set_enabled(self, device_id: str, dataset: str, enabled: bool) -> None:
        if dataset not in DATASETS:
            raise SyncError(400, "unknown_dataset", f"'{dataset}' cannot be synced. Choices: {', '.join(DATASETS)}.")
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO sync_prefs (device_id, dataset, enabled, updated_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(device_id, dataset) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
                         (device_id, dataset, int(enabled), _now()))

    def enabled(self, device_id: str) -> list[str]:
        with self._db.connect() as conn:
            return [r[0] for r in conn.execute("SELECT dataset FROM sync_prefs WHERE device_id = ? AND enabled = 1 ORDER BY dataset",
                                               (device_id,))]

    def status(self, device_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = {r["dataset"]: dict(r) for r in conn.execute(
                "SELECT dataset, enabled, last_sync_at, last_error FROM sync_prefs WHERE device_id = ?", (device_id,))}
        return [{"dataset": d, "enabled": bool(rows.get(d, {}).get("enabled", 0)),
                 "last_sync_at": rows.get(d, {}).get("last_sync_at"), "last_error": rows.get(d, {}).get("last_error", "")}
                for d in DATASETS]

    def forget_device(self, device_id: str) -> int:
        """Revocation / "delete my sync data": preferences and the phone's operation history go. Returns rows removed."""
        with self._db.transaction() as conn:
            removed = conn.execute("DELETE FROM sync_prefs WHERE device_id = ?", (device_id,)).rowcount
            removed += conn.execute("DELETE FROM sync_ops WHERE device_id = ?", (device_id,)).rowcount
        return removed

    def _require(self, device_id: str, dataset: str) -> None:
        if dataset not in DATASETS:
            raise SyncError(400, "unknown_dataset", f"'{dataset}' cannot be synced.")
        if dataset not in self.enabled(device_id):
            raise SyncError(403, "not_enabled", f"Syncing '{dataset}' is switched off for this phone. Turn it on on the PC "
                                                "(Jarvis: 'sync to-dos with my phone').")

    def _mark(self, device_id: str, dataset: str, error: str = "") -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE sync_prefs SET last_sync_at = ?, last_error = ? WHERE device_id = ? AND dataset = ?",
                         (_now(), error[:200], device_id, dataset))

    # ------------------------------------------------------------------------------------ pull

    def _snapshot(self, conn) -> tuple[list[dict[str, Any]], int]:
        latest = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_log").fetchone()[0]
        out = []
        for row in conn.execute("SELECT id, text, due, done, done_at, minutes FROM todos ORDER BY id"):
            rev = conn.execute("SELECT COALESCE(MAX(rev), 1) FROM sync_log WHERE dataset = 'todos' AND record_id = ?",
                               (row["id"],)).fetchone()[0]
            out.append({"id": row["id"], "op": "upsert", "rev": rev, "fields": {
                "text": row["text"], "due": row["due"], "done": bool(row["done"]), "done_at": row["done_at"],
                "minutes": row["minutes"]}})
        return out, latest

    def pull(self, device_id: str, dataset: str, since: int = 0, limit: int = MAX_PULL) -> dict[str, Any]:
        """Changes after ``since`` (0 = a full snapshot). ``next`` is what to send as ``since`` next time."""
        self._require(device_id, "todos" if dataset == "todos" else dataset)
        if dataset != "todos":
            raise SyncError(400, "not_a_data_set", "Use the notifications command for notifications.")
        limit = max(1, min(int(limit), MAX_PULL))
        with self._db.connect() as conn:
            oldest = conn.execute("SELECT COALESCE(MIN(seq), 0) FROM sync_log").fetchone()[0]
            latest = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_log").fetchone()[0]
            if since <= 0 or (oldest and since < oldest - 1):
                items, latest = self._snapshot(conn)
                result = {"reset": since > 0, "snapshot": True, "changes": items, "next": latest, "more": False}
            else:
                rows = conn.execute("SELECT seq, record_id, op, rev, payload FROM sync_log WHERE dataset = 'todos' AND seq > ? "
                                    "ORDER BY seq LIMIT ?", (since, limit)).fetchall()
                newest: dict[int, Any] = {}
                for r in rows:
                    newest[r["record_id"]] = r         # several edits of one record collapse to the latest
                changes = [{"id": r["record_id"], "op": r["op"], "rev": r["rev"],
                            "fields": json.loads(r["payload"]) if r["op"] == "upsert" else {}} for r in newest.values()]
                for c in changes:
                    if c["op"] == "upsert":
                        c["fields"]["done"] = bool(c["fields"].get("done"))
                nxt = rows[-1]["seq"] if rows else since
                result = {"reset": False, "snapshot": False, "changes": changes, "next": nxt, "more": nxt < latest}
        self._mark(device_id, dataset)
        self._prune()
        return result

    def _prune(self) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM sync_log WHERE seq <= (SELECT COALESCE(MAX(seq), 0) - ? FROM sync_log)", (KEEP_LOG,))

    # ------------------------------------------------------------------------------------ push

    @staticmethod
    def _clean(fields: Any) -> dict[str, Any]:
        if not isinstance(fields, dict):
            raise SyncError(400, "malformed", "fields must be an object")
        unknown = set(fields) - set(TODO_FIELDS)
        if unknown:
            raise SyncError(400, "unknown_field", f"Cannot sync: {', '.join(sorted(unknown))}.")
        clean: dict[str, Any] = {}
        if "text" in fields:
            text = fields["text"]
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300:
                raise SyncError(400, "invalid_text", "text must be 1-300 characters")
            clean["text"] = text.strip()
        if "due" in fields:
            due = fields["due"]
            if due is not None and (not isinstance(due, str) or len(due) > 40):
                raise SyncError(400, "invalid_due", "due must be a short text or null")
            clean["due"] = due
        if "done" in fields:
            if not isinstance(fields["done"], bool):
                raise SyncError(400, "invalid_done", "done must be true or false")
            clean["done"] = int(fields["done"])
        if "minutes" in fields:
            minutes = fields["minutes"]
            if minutes is not None and (not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 1440):
                raise SyncError(400, "invalid_minutes", "minutes must be 1-1440 or null")
            clean["minutes"] = minutes
        return clean

    def push(self, device_id: str, dataset: str, changes: Any) -> dict[str, Any]:
        self._require(device_id, dataset)
        if dataset != "todos":
            raise SyncError(400, "not_a_data_set", "Only to-dos can be pushed.")
        if not isinstance(changes, list) or not changes or len(changes) > MAX_CHANGES:
            raise SyncError(400, "malformed", f"Send 1-{MAX_CHANGES} changes.")
        results = [self._push_one(device_id, change) for change in changes]
        self._mark(device_id, dataset, "" if all(r["status"] in ("applied", "duplicate") for r in results) else "some changes were not applied")
        return {"results": results}

    def _push_one(self, device_id: str, change: Any) -> dict[str, Any]:
        if not isinstance(change, dict) or not isinstance(change.get("op_id"), str) or not _OP_ID.match(change["op_id"]):
            return {"op_id": str(change.get("op_id", ""))[:64] if isinstance(change, dict) else "", "status": "rejected",
                    "reason": "each change needs an op_id of 8-64 letters, digits, - or _"}
        op_id = change["op_id"]
        try:
            with self._db.transaction() as conn:
                saved = conn.execute("SELECT result FROM sync_ops WHERE device_id = ? AND op_id = ?", (device_id, op_id)).fetchone()
                if saved:
                    return {**json.loads(saved[0]), "status": "duplicate"}
                result = self._apply(conn, change)
                conn.execute("INSERT INTO sync_ops (device_id, op_id, result, applied_at) VALUES (?, ?, ?, ?)",
                             (device_id, op_id, json.dumps(result), _now()))   # same transaction: applied <=> remembered
                return result
        except SyncError as exc:
            return {"op_id": op_id, "status": "rejected", "reason": str(exc)}

    def _current_rev(self, conn, record_id: int) -> int:
        return conn.execute("SELECT COALESCE(MAX(rev), 1) FROM sync_log WHERE dataset = 'todos' AND record_id = ?",
                            (record_id,)).fetchone()[0]

    def _server_version(self, conn, record_id: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT id, text, due, done, done_at, minutes FROM todos WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "rev": self._current_rev(conn, record_id), "fields": {
            "text": row["text"], "due": row["due"], "done": bool(row["done"]), "done_at": row["done_at"], "minutes": row["minutes"]}}

    def _apply(self, conn, change: dict[str, Any]) -> dict[str, Any]:
        op, op_id = change.get("op"), change["op_id"]
        if op == "create":
            fields = self._clean(change.get("fields"))
            if "text" not in fields:
                raise SyncError(400, "invalid_text", "a new to-do needs text")
            cursor = conn.execute("INSERT INTO todos (text, due, done, created_at, minutes) VALUES (?, ?, ?, ?, ?)",
                                  (fields["text"], fields.get("due"), fields.get("done", 0), _now(), fields.get("minutes")))
            record_id = int(cursor.lastrowid)
            return {"op_id": op_id, "status": "applied", "record_id": record_id, "rev": self._current_rev(conn, record_id),
                    "client_ref": str(change.get("client_ref", ""))[:64]}
        record_id, base = change.get("record_id"), change.get("base_rev")
        if not isinstance(record_id, int) or isinstance(record_id, bool) or not isinstance(base, int) or isinstance(base, bool):
            raise SyncError(400, "malformed", "update and delete need record_id and base_rev")
        server = self._server_version(conn, record_id)
        if server is None:
            return {"op_id": op_id, "status": "conflict", "record_id": record_id, "reason": "deleted on the PC", "server": None}
        if base != server["rev"]:
            return {"op_id": op_id, "status": "conflict", "record_id": record_id, "reason": "changed on the PC since you "
                    "last synced; your edit was NOT applied", "server": server}
        if op == "delete":
            conn.execute("DELETE FROM todos WHERE id = ?", (record_id,))
            return {"op_id": op_id, "status": "applied", "record_id": record_id, "rev": self._current_rev(conn, record_id)}
        if op != "update":
            raise SyncError(400, "unknown_op", "op must be create, update or delete")
        fields = self._clean(change.get("fields"))
        if not fields:
            raise SyncError(400, "malformed", "nothing to update")
        sets = ", ".join(f"{k} = ?" for k in fields)
        extra = ""
        if "done" in fields:
            extra = ", done_at = " + ("?" if fields["done"] else "NULL")
        params = [*fields.values()] + ([_now()] if "done" in fields and fields["done"] else []) + [record_id]
        conn.execute(f"UPDATE todos SET {sets}{extra} WHERE id = ?", params)
        return {"op_id": op_id, "status": "applied", "record_id": record_id, "rev": self._current_rev(conn, record_id)}

    # ------------------------------------------------------------------------------ notifications

    def notifications(self, device_id: str, since: int = 0, limit: int = 30) -> dict[str, Any]:
        """Generic one-line notices for events after ``since`` (an events-table id). Polling only."""
        self._require(device_id, "notifications")
        names = tuple(NOTIFICATION_TEXT)
        marks = ",".join("?" for _ in names)
        with self._db.connect() as conn:
            rows = conn.execute(f"SELECT id, ts, name FROM events WHERE id > ? AND name IN ({marks}) ORDER BY id LIMIT ?",
                                (max(0, int(since)), *names, max(1, min(int(limit), 50)))).fetchall()
            latest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        items = [{"id": r["id"], "kind": r["name"], "text": NOTIFICATION_TEXT[r["name"]], "at": r["ts"]} for r in rows]
        self._mark(device_id, "notifications")
        return {"notifications": items, "next": items[-1]["id"] if items else (since if since else latest),
                "delivery": "polling"}
