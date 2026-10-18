"""Universal undo: "undo your last action" / "undo that".

After an action that can be reversed succeeds, the executor records its **inverse as an ordinary tool
call** (move the file back, rename it back, set the volume to what it was...). Undoing runs that call
through the executor like anything else, so it is validated, permission-checked and audited - moving a new
file to the Recycle Bin still asks first. Nothing is ever undone automatically.

Only actions whose inverse is exact are recorded. Opening an app, sending a message, deleting to the Recycle
Bin (restore it from there), running a command - those can't be undone by Jarvis, and it says so.
"""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.ai.base import ToolCall
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:
    from app.memory.database import Database
    from app.tools.base import ToolResult

_log = get_logger("core.undo")
KEEP_HOURS = 24  # older actions aren't offered any more (the world has moved on)
UNDOING: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_undoing", default=False)


@dataclass(frozen=True)
class Inverse:
    call: ToolCall
    description: str  # what undoing does, e.g. "move report.pdf back to Downloads"


def inverse_of(call: ToolCall, result: "ToolResult") -> Inverse | None:
    """The exact reverse of a successful call, or None when there isn't one."""
    if not result.ok:
        return None
    data = result.data or {}
    args = call.arguments if isinstance(call.arguments, dict) else {}
    path, old = data.get("path"), data.get("old_path")
    if call.name == "move_item" and path:
        if args.get("mode", "move") == "copy":
            return Inverse(ToolCall("delete_item", {"path": path}), f"move the copy {Path(path).name} to the Recycle Bin")
        if old:
            return Inverse(ToolCall("move_item", {"source": path, "destination": str(Path(old).parent)}),
                           f"move {Path(path).name} back to {Path(old).parent.name or Path(old).parent}")
    if call.name == "rename_item" and path and old:
        return Inverse(ToolCall("rename_item", {"path": path, "new_name": Path(old).name}),
                       f"rename {Path(path).name} back to {Path(old).name}")
    if call.name == "batch_rename" and args.get("apply") and data.get("renamed"):
        pairs = [[new_path, old_name] for new_path, old_name in data["renamed"]]
        return Inverse(ToolCall("batch_rename", {"pairs": pairs, "apply": True}),
                       f"give the {len(pairs)} renamed file(s) their old names back")
    if call.name == "edit_code" and data.get("backup"):
        name = Path(str(path)).name if path else "the file"
        return Inverse(ToolCall("restore_code", {"backup": data["backup"]}),
                       f"{'remove the new' if data.get('created') else 'restore'} {name} (before my change)")
    if call.name in ("zip_items", "extract_zip", "merge_pdfs", "convert_image", "word_to_pdf") and path:
        return Inverse(ToolCall("delete_item", {"path": path}), f"move the new {Path(path).name} to the Recycle Bin")
    if call.name == "create_item" and path:
        return Inverse(ToolCall("delete_item", {"path": path}), f"move the new {Path(path).name} to the Recycle Bin")
    if call.name in ("volume_control", "brightness_control") and "previous" in data \
            and args.get("action") in ("set", "up", "down"):
        what = "volume" if call.name == "volume_control" else "brightness"
        return Inverse(ToolCall(call.name, {"action": "set", "level": int(data["previous"])}),
                       f"set the {what} back to {int(data['previous'])}%")
    if call.name == "device_toggle" and data.get("previous") in ("on", "off") and args.get("state") in ("on", "off") \
            and data.get("previous") != args.get("state"):
        name = {"wifi": "Wi-Fi", "bluetooth": "Bluetooth", "dark_mode": "dark mode"}.get(args.get("setting"), "it")
        return Inverse(ToolCall("device_toggle", {"setting": args["setting"], "state": data["previous"]}),
                       f"turn {name} back {data['previous']}")
    return None


class UndoStore:
    def __init__(self, database: "Database") -> None:
        self._db = database

    def record(self, call: ToolCall, inverse: Inverse, run_id: str | None) -> None:
        from app.memory.database import utc_now_iso

        try:
            with self._db.transaction() as conn:
                conn.execute("INSERT INTO undo_records (run_id, created_at, tool, description, inverse_tool, "
                             "inverse_args) VALUES (?, ?, ?, ?, ?, ?)",
                             (run_id, utc_now_iso(), call.name, inverse.description[:300], inverse.call.name,
                              json.dumps(dict(inverse.call.arguments), ensure_ascii=False)))
                conn.execute("DELETE FROM undo_records WHERE id NOT IN (SELECT id FROM undo_records ORDER BY id DESC "
                             "LIMIT 200)")
        except Exception:
            _log.warning("could not record how to undo the action", exc_info=True)

    def pending(self, limit: int = 10, now: datetime | None = None) -> list[dict[str, Any]]:
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(hours=KEEP_HOURS)).isoformat()
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM undo_records WHERE undone = 0 AND created_at >= ? ORDER BY id DESC "
                                "LIMIT ?", (cutoff, limit)).fetchall()
        return [dict(r) for r in rows]

    def mark_undone(self, record_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE undo_records SET undone = 1 WHERE id = ?", (record_id,))

    @staticmethod
    def call_of(record: dict[str, Any]) -> ToolCall:
        return ToolCall(record["inverse_tool"], json.loads(record["inverse_args"]))
