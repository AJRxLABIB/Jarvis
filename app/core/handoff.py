"""Cross-device task handoff: a mission moves to a paired device (or back) as a small, signed, expiring package.

What travels: the goal and mission id, which steps are done, the checkpoint (the next step), a short redacted context
summary, file NAMES (never contents or full paths), pending approvals, the next recommended action, which tools the rest of
the job needs, and an expiry. What never travels: step arguments, credentials, keys, tokens, or any text the redaction
rules recognise as a secret - a package containing one is refused on both ends.

The receiver (this PC, when the phone sends a package back or asks to resume) validates, in order: well-formed and small ->
issued by this PC and unchanged (HMAC with a key that never leaves the database, plus the stored digest) -> not expired ->
meant for this device -> not revoked and the device still trusted -> not already used. It then checks the mission's REAL
state: if the mission moved on since the package was made it is a conflict and nothing resumes. A valid package does NOT run
anything: it yields a resume plan, and continuing still goes through ``resume_mission`` with all its approvals.

Every package is single-use (an atomic state change, so two devices cannot both resume it). The phone app does not yet have a
screen for this - the protocol and the PC side are built and tested; the phone side needs a JARVIS Mobile release.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any

from app.memory.settings_store import SettingsStore
from app.tools.base import ToolError
from app.utils.redaction import redact

VERSION = 1
DEFAULT_TTL_MINUTES = 30
MAX_TTL_MINUTES = 24 * 60
MAX_BYTES = 16_000
MAX_STEPS = 50
KEY_NAME = "handoff.key"
_REQUIRED = ("v", "id", "mission_id", "title", "device_id", "created", "expires", "steps", "checkpoint", "mac")
_DONE = ("done", "skipped", "undone")
# only fields that hold human text are scanned for secrets: identifiers (ids, timestamps) legitimately contain long digit runs
# that the card-number heuristic in the redaction rules would flag by chance (a mission id was refused about 1 time in 10)
FREE_TEXT = ("title", "next", "context", "pending_approvals", "references", "steps")


class HandoffError(ToolError):
    """A handoff was refused; ``code`` says why (malformed, bad_mac, unknown, expired, wrong_device, revoked, consumed, secret, conflict)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps({k: v for k, v in body.items() if k != "mac"}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _key(db: Any) -> bytes:
    store = SettingsStore(db)
    stored = store.get(KEY_NAME)
    if not stored:
        stored = base64.b64encode(secrets.token_bytes(32)).decode()
        store.set(KEY_NAME, stored)
    return base64.b64decode(stored)


def _sign(db: Any, body: dict[str, Any]) -> str:
    return base64.b64encode(hmac.new(_key(db), _canonical(body), hashlib.sha256).digest()).decode()


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(body)).hexdigest()


def _clean(text: str, limit: int) -> str:
    return redact(" ".join(str(text).split()))[:limit]


def _names(step: Any) -> list[str]:
    """File NAMES a step refers to (never full paths, never contents)."""
    out = []
    for key in ("path", "file", "source", "destination", "folder"):
        value = (getattr(step, "arguments", {}) or {}).get(key)
        if isinstance(value, str) and value:
            out.append(PurePath(value.replace("\\", "/")).name[:80])
    return out


@dataclass
class ResumePlan:
    mission_id: str
    title: str
    next_step: str
    pending_approvals: list[str]
    needs: list[str]
    note: str = "Nothing has run. Continue with 'resume my mission': every approval is asked again."

    def describe(self) -> str:
        lines = [f"Resume '{self.title}' at: {self.next_step}"]
        if self.pending_approvals:
            lines.append("Waiting for your approval: " + "; ".join(self.pending_approvals))
        if self.needs:
            lines.append("The rest uses: " + ", ".join(self.needs))
        return "\n".join(lines + [self.note])


@dataclass
class Conflict:
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return "This handoff is out of date, so nothing resumed: " + "; ".join(self.reasons)


# ------------------------------------------------------------------ issuing


def create(db: Any, mission: Any, device_id: str, ttl_minutes: int = DEFAULT_TTL_MINUTES, now: datetime | None = None,
           registry: Any = None) -> dict[str, Any]:
    """Package ``mission`` for ``device_id`` (which must be a trusted paired device when a registry is given)."""
    if mission.state in ("completed", "cancelled", "rolled_back"):
        raise HandoffError("That mission is finished; there is nothing to hand over.", "finished")
    if registry is not None:
        device = registry.get(device_id)
        if device is None or device.trust != "trusted":
            raise HandoffError("That device isn't a trusted, paired device.", "wrong_device")
    ttl = max(1, min(int(ttl_minutes), MAX_TTL_MINUTES))
    now = now or _now()
    steps = list(mission.steps)[:MAX_STEPS]
    remaining = [s for s in steps if s.state not in _DONE]
    nxt = remaining[0] if remaining else None
    body: dict[str, Any] = {
        "v": VERSION, "id": secrets.token_urlsafe(16), "mission_id": mission.id, "title": _clean(mission.title, 200),
        "state": mission.state, "device_id": device_id,
        "created": now.isoformat(timespec="seconds"), "expires": (now + timedelta(minutes=ttl)).isoformat(timespec="seconds"),
        "steps": [{"n": s.n, "title": _clean(s.title, 120), "tool": s.tool, "state": s.state} for s in steps],
        "completed": [s.n for s in steps if s.state in _DONE],
        "checkpoint": nxt.n if nxt else None,
        "next": _clean(nxt.title, 160) if nxt else "nothing left: it only needs verifying",
        "pending_approvals": [_clean(s.title, 120) for s in remaining if s.checkpoint],
        "needs": sorted({s.tool for s in remaining}),
        "references": sorted({n for s in remaining for n in _names(s)})[:20],
        "context": _clean(mission.note or "", 500),
    }
    body["mac"] = _sign(db, body)
    if len(json.dumps(body)) > MAX_BYTES:
        raise HandoffError("That mission is too large to hand over.", "too_large")
    with db.transaction() as conn:
        conn.execute("INSERT INTO handoffs (id, mission_id, device_id, digest, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (body["id"], mission.id, device_id, _digest(body), body["created"], body["expires"]))
    return body


# ------------------------------------------------------------------ validating


def _secret_in(value: Any) -> bool:
    if isinstance(value, str):
        return redact(value) != value
    if isinstance(value, dict):
        return any(_secret_in(v) or _secret_in(k) for k, v in value.items())
    if isinstance(value, list):
        return any(_secret_in(v) for v in value)
    return False


def validate(db: Any, package: Any, device_id: str | None = None, now: datetime | None = None, registry: Any = None) -> dict[str, Any]:
    """The package as a dict when every check passes; otherwise a HandoffError with the reason. Does not consume it."""
    if isinstance(package, (str, bytes)):
        if len(package) > MAX_BYTES * 2:
            raise HandoffError("The handoff is too large.", "malformed")
        try:
            package = json.loads(package)
        except ValueError as error:
            raise HandoffError("The handoff isn't valid JSON.", "malformed") from error
    if not isinstance(package, dict) or any(k not in package for k in _REQUIRED):
        raise HandoffError("The handoff is incomplete.", "malformed")
    if package["v"] != VERSION:
        raise HandoffError(f"Handoff version {package['v']!r} isn't supported (this PC speaks v{VERSION}).", "malformed")
    if not isinstance(package["steps"], list) or len(package["steps"]) > MAX_STEPS or len(json.dumps(package)) > MAX_BYTES:
        raise HandoffError("The handoff is too large or its steps are malformed.", "malformed")
    if not isinstance(package["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", package["id"]):
        raise HandoffError("The handoff has a bad identifier.", "malformed")
    expected = _sign(db, package)
    if not hmac.compare_digest(expected, str(package["mac"])):
        raise HandoffError("The handoff was changed or didn't come from this PC.", "bad_mac")
    if _secret_in({k: package[k] for k in FREE_TEXT if k in package}):
        raise HandoffError("The handoff contains something that looks like a secret, so it was refused.", "secret")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (package["id"],)).fetchone()
    if row is None or row["digest"] != _digest(package):
        raise HandoffError("This PC never issued that handoff.", "unknown")
    now = now or _now()
    try:
        expires = datetime.fromisoformat(package["expires"])
    except ValueError as error:
        raise HandoffError("The handoff's expiry is unreadable.", "malformed") from error
    if now > expires:
        raise HandoffError("The handoff has expired. Create a new one.", "expired")
    if device_id is not None and package["device_id"] != device_id:
        raise HandoffError("The handoff was made for a different device.", "wrong_device")
    if row["state"] == "revoked":
        raise HandoffError("The handoff was revoked.", "revoked")
    if row["state"] == "accepted":
        raise HandoffError("The handoff was already used.", "consumed")
    if registry is not None:
        device = registry.get(package["device_id"])
        if device is None or device.trust != "trusted":
            raise HandoffError("The device is no longer trusted (revoked or removed).", "revoked")
    return package


def accept(db: Any, package: Any, missions: Any, device_id: str | None = None, now: datetime | None = None,
           registry: Any = None) -> ResumePlan | Conflict:
    """Validate, then compare with the mission's real state. Valid and current -> consumed and a ResumePlan; stale -> Conflict
    (not consumed, nothing resumes). Never runs a step."""
    pkg = validate(db, package, device_id, now, registry)
    try:
        mission = missions.get(pkg["mission_id"])
    except ToolError as error:
        raise HandoffError("The mission no longer exists on this PC.", "conflict") from error
    reasons = []
    if mission.state in ("completed", "cancelled", "rolled_back"):
        reasons.append(f"the mission is now {mission.state}")
    actual = [s.n for s in mission.steps if s.state in _DONE]
    if actual != pkg["completed"]:
        reasons.append(f"the finished steps changed (package: {pkg['completed']}, now: {actual})")
    if reasons:
        return Conflict(reasons)
    with db.transaction() as conn:                                         # single use, even with two devices racing
        changed = conn.execute("UPDATE handoffs SET state = 'accepted', accepted_at = ? WHERE id = ? AND state = 'issued'",
                               ((now or _now()).isoformat(timespec="seconds"), pkg["id"])).rowcount
    if changed != 1:
        raise HandoffError("The handoff was already used.", "consumed")
    return ResumePlan(pkg["mission_id"], pkg["title"], pkg["next"], list(pkg["pending_approvals"]), list(pkg["needs"]))


# ------------------------------------------------------------------ housekeeping


def revoke(db: Any, handoff_id: str = "", device_id: str = "") -> int:
    """Revoke one package, or every unused package for a device. Returns how many."""
    if not handoff_id and not device_id:
        raise HandoffError("Give a handoff id or a device.", "missing_id")
    sql, arg = ("id = ?", handoff_id) if handoff_id else ("device_id = ?", device_id)
    with db.transaction() as conn:
        return conn.execute(f"UPDATE handoffs SET state = 'revoked' WHERE {sql} AND state = 'issued'", (arg,)).rowcount


def listing(db: Any, now: datetime | None = None, active_only: bool = True) -> list[dict[str, Any]]:
    now_iso = (now or _now()).isoformat(timespec="seconds")
    with db.connect() as conn:
        rows = conn.execute("SELECT id, mission_id, device_id, state, created_at, expires_at, accepted_at FROM handoffs "
                            "ORDER BY created_at DESC LIMIT 50").fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["effective"] = "expired" if r["state"] == "issued" and r["expires_at"] < now_iso else r["state"]
    return [r for r in out if r["effective"] == "issued"] if active_only else out
