"""PC -> phone actions and phone -> PC notices, over the existing signed JARVIS Mobile channel.

* The PC queues an action for a paired, trusted phone (flashlight, ring, open an app or a link, show a message).
  The phone collects it the next time it checks in (every few seconds while the app is open; in the background only
  when background checks are on) and reports the result. Actions expire after EXPIRE_SECONDS, so a phone that comes
  back online hours later doesn't suddenly start ringing.
* The phone must have switched "Let my PC control this phone" on itself - the PC can't turn that on.
* The phone can forward notifications (apps the user picked on the phone) and share text / links to the PC. Codes
  that look like one-time passwords are removed before anything is stored.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

ACTIONS = ("flashlight_on", "flashlight_off", "ring", "open_app", "open_link", "show_text")
EXPIRE_SECONDS = 120
MAX_TEXT = 500
_OTP = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
_URL = re.compile(r"^https?://[^\s]{1,1900}$", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def redact_codes(text: str) -> str:
    """'Your code is 482913' -> 'Your code is ••••' (verification codes never leave the phone's notice)."""
    return _OTP.sub("••••", text or "")


def check_action(action: str, args: dict[str, Any]) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError(f"Unknown phone action '{action}'.")
    clean: dict[str, Any] = {}
    if action == "open_app":
        name = str(args.get("app", "")).strip()[:60]
        if not name:
            raise ValueError("Which app?")
        clean["app"] = name
    elif action == "open_link":
        url = str(args.get("url", "")).strip()
        if not _URL.match(url):
            raise ValueError("Only http(s) links can be opened on the phone.")
        clean["url"] = url
    elif action == "show_text":
        text = str(args.get("text", "")).strip()[:MAX_TEXT]
        if not text:
            raise ValueError("What should the phone show?")
        clean["text"] = text
    elif action == "ring":
        clean["seconds"] = max(3, min(int(args.get("seconds", 15) or 15), 60))
    return clean


class PhoneLink:
    def __init__(self, database) -> None:
        self.db = database

    # ---- PC side
    def queue(self, device_id: str, action: str, args: dict[str, Any] | None = None) -> int:
        clean = check_action(action, args or {})
        now = _now()
        with self.db.connect() as conn:
            cur = conn.execute("INSERT INTO phone_commands (device_id, action, args, created, expires) VALUES (?, ?, ?, ?, ?)",
                               (device_id, action, json.dumps(clean), _iso(now),
                                _iso(now + timedelta(seconds=EXPIRE_SECONDS))))
            return int(cur.lastrowid)

    def outcome(self, command_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT status, result, expires FROM phone_commands WHERE id = ?", (command_id,)).fetchone()
        if row is None:
            return None
        status = row["status"]
        if status in ("pending", "sent") and row["expires"] < _iso(_now()):
            status = "expired"
        return {"status": status, "result": row["result"]}

    def wait(self, command_id: int, seconds: float = 8.0, sleep=time.sleep) -> dict[str, Any]:
        end = time.monotonic() + seconds
        while True:
            outcome = self.outcome(command_id) or {"status": "expired", "result": ""}
            if outcome["status"] in ("done", "failed", "expired") or time.monotonic() >= end:
                return outcome
            sleep(0.3)

    def state(self, device_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT seen, state FROM phone_state WHERE device_id = ?", (device_id,)).fetchone()
        if row is None:
            return {}
        try:
            data = json.loads(row["state"])
        except ValueError:
            data = {}
        return {"seen": row["seen"], **data}

    # ---- phone side (called by the remote server for a verified device)
    def collect(self, device_id: str, report: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """The phone checks in: store what it reports, hand over (and mark sent) its unexpired actions."""
        now = _iso(_now())
        state = {k: report[k] for k in ("battery", "charging", "control", "notifications", "app_version")
                 if isinstance(report, dict) and k in report}
        previous = {k: v for k, v in self.state(device_id).items() if k != "seen"}
        state = {**previous, **state}                     # a bare check-in keeps what the phone said last time
        with self.db.connect() as conn:
            conn.execute("INSERT INTO phone_state (device_id, seen, state) VALUES (?, ?, ?) ON CONFLICT(device_id) "
                         "DO UPDATE SET seen = excluded.seen, state = excluded.state", (device_id, now, json.dumps(state)))
            conn.execute("UPDATE phone_commands SET status = 'expired' WHERE device_id = ? AND status IN ('pending', "
                         "'sent') AND expires < ?", (device_id, now))
            rows = conn.execute("SELECT id, action, args FROM phone_commands WHERE device_id = ? AND status = 'pending' "
                                "ORDER BY id LIMIT 5", (device_id,)).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.execute(f"UPDATE phone_commands SET status = 'sent' WHERE id IN ({','.join('?' * len(ids))})", ids)
        return [{"id": r["id"], "action": r["action"], "args": json.loads(r["args"])} for r in rows]

    def report(self, device_id: str, command_id: int, ok: bool, message: str = "") -> bool:
        with self.db.connect() as conn:
            changed = conn.execute("UPDATE phone_commands SET status = ?, result = ? WHERE id = ? AND device_id = ? AND "
                                   "status = 'sent'", ("done" if ok else "failed", str(message)[:300], command_id,
                                                       device_id)).rowcount
        return changed > 0                                # only its own, only once (no replays, no other phone's)
