"""Approval system 2.0: standing approvals ("grants") - "don't ask me again about this for a while".

A grant lets one MODERATE action run without the confirmation dialog, for a limited time. The limits are fixed
here, in code, and nothing the model says can loosen them:

* only MODERATE actions - DANGEROUS always asks, CRITICAL is always blocked (the permission levels stay the law);
* never for actions that send things out (email, messages, websites), store things (memories, automations) or are
  on the NEVER_GRANT list (deleting, power, code changes...);
* never when the request was steered by untrusted text (the taint firewall still asks);
* never while any emergency switch is on; an emergency stop revokes every grant;
* always time-limited (at most 12 hours) and revocable; "session" grants end when Jarvis restarts;
* optionally narrowed to exact argument values ("volume up" but not "volume set 100").

Creating a grant is itself a DANGEROUS action, so you always confirm it in the window or with your phone's
fingerprint - the model can suggest one, but only you can switch it on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.security.permissions import RiskLevel

MAX_MINUTES = 12 * 60
SCOPES = ("session", "minutes")
NEVER_GRANT = frozenset({
    "delete_item", "empty_recycle_bin", "power_control", "close_all_apps", "end_process", "edit_code",
    "safe_edit", "restore_code", "run_command", "run_project", "git_commit", "apply_proposal", "rollback_proposal",
    "change_setting", "forget", "forget_everything", "forget_history", "manage_memory", "emergency_control",
    "grant_approval", "windows_update", "device_toggle", "wake_device", "type_text", "press_keys",
})
_SESSION_STARTED = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def grantable(tool: str, category: str, level: RiskLevel) -> tuple[bool, str]:
    """Can a standing approval ever cover this tool? Deterministic."""
    from app.security.taint import PERSISTENT_TOOLS, is_egress

    if level != RiskLevel.MODERATE:
        return False, "only moderate actions can be pre-approved; dangerous ones always ask"
    if tool in NEVER_GRANT:
        return False, f"{tool} always asks"
    if is_egress(tool, category):
        return False, f"{tool} sends things out, so it always asks"
    if tool in PERSISTENT_TOOLS:
        return False, f"{tool} saves things, so it always asks"
    return True, ""


@dataclass
class Grant:
    id: int
    tool: str
    match: dict[str, Any]
    scope: str
    created_at: str
    expires_at: str
    uses: int
    revoked_at: str | None = None
    note: str = ""

    @property
    def active(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.scope == "session" and self.created_at < _SESSION_STARTED:
            return False  # from an earlier run of Jarvis
        return self.expires_at > _now().isoformat(timespec="seconds")

    def covers(self, tool: str, arguments: dict[str, Any]) -> bool:
        if tool != self.tool or not self.active:
            return False
        return all(arguments.get(k) == v for k, v in self.match.items())

    def describe(self) -> str:
        what = self.tool + (" (" + ", ".join(f"{k}={v}" for k, v in self.match.items()) + ")" if self.match else "")
        until = "until Jarvis restarts" if self.scope == "session" else f"until {self.expires_at[11:16]} UTC"
        return f"#{self.id} {what} - {until}, used {self.uses} time(s)"


class GrantStore:
    def __init__(self, database) -> None:
        self._db = database

    @staticmethod
    def _row(r) -> Grant:
        return Grant(r["id"], r["tool"], json.loads(r["match"] or "{}"), r["scope"], r["created_at"],
                     r["expires_at"], r["uses"], r["revoked_at"], r["note"])

    def add(self, tool: str, *, minutes: int | None, match: dict[str, Any] | None = None, note: str = "") -> Grant:
        scope = "session" if minutes is None else "minutes"
        span = MAX_MINUTES if minutes is None else max(1, min(int(minutes), MAX_MINUTES))
        now = _now()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO approval_grants (tool, match, scope, created_at, expires_at, note) VALUES (?,?,?,?,?,?)",
                (tool, json.dumps(match or {}, sort_keys=True), scope, now.isoformat(timespec="seconds"),
                 (now + timedelta(minutes=span)).isoformat(timespec="seconds"), note[:200]))
            grant_id = cursor.lastrowid
        return self.get(grant_id)  # type: ignore[return-value]

    def get(self, grant_id: int) -> Grant | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM approval_grants WHERE id = ?", (grant_id,)).fetchone()
        return self._row(row) if row else None

    def active(self) -> list[Grant]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM approval_grants WHERE revoked_at IS NULL ORDER BY id DESC").fetchall()
        return [g for g in (self._row(r) for r in rows) if g.active]

    def find(self, tool: str, arguments: dict[str, Any]) -> Grant | None:
        return next((g for g in self.active() if g.covers(tool, arguments)), None)

    def used(self, grant: Grant) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE approval_grants SET uses = uses + 1 WHERE id = ?", (grant.id,))

    def revoke(self, grant_id: int) -> bool:
        with self._db.transaction() as conn:
            return conn.execute("UPDATE approval_grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                                (_now().isoformat(timespec="seconds"), grant_id)).rowcount > 0

    def revoke_all(self) -> int:
        with self._db.transaction() as conn:
            return conn.execute("UPDATE approval_grants SET revoked_at = ? WHERE revoked_at IS NULL",
                                (_now().isoformat(timespec="seconds"),)).rowcount
