"""What a paired phone can do, in three separate columns - never merged into one "supported" word.

* declared      - the PC knows the action and the phone's app version knows it too (not answered "unsupported");
* granted       - the permission the action needs is switched on, on the phone (control / calls and messages / lock);
* demonstrated  - the phone really answered "done" to this action, recently. Only this proves it works.

Pure logic over data the PC already keeps (``phone_state`` reports and ``phone_commands`` answers); nothing here talks to the
phone. ``explain`` turns a "no" into the specific limitation and a supported alternative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

STALE_AFTER = timedelta(minutes=10)         # the phone checks in regularly while its app runs; longer = out of reach
PROOF_VALID = timedelta(days=30)

# which phone-side permission each action needs ("control" = the master switch every action needs)
NEEDS: dict[str, tuple[str, ...]] = {
    "call": ("control", "comms"), "end_call": ("control", "comms"), "send_sms": ("control", "comms"),
    "read_messages": ("control", "comms"), "find_contact": ("control", "comms"),
    "lock_phone": ("control", "lock"),
}
DEFAULT_NEEDS = ("control",)
# one-time user action on the phone Android requires every time; JARVIS cannot do it for you
CONSENT_EACH_TIME = frozenset({"share_screen"})
# receiving needs the phone app to check in; these do not go through the command queue
LOCAL_ONLY = frozenset({"received_files", "status", "cancel_pending"})

FIX = {
    "control": "on the phone: JARVIS Mobile > Settings > 'Let my PC control this phone'",
    "comms": "on the phone: JARVIS Mobile > Control > 'Allow calls and messages' (and Android's own permissions)",
    "lock": "on the phone: Settings > Security > Device admin apps > JARVIS Mobile (the only power is 'lock the screen')",
}
ALTERNATIVE = {
    "lock_phone": "ring the phone (find my phone), or lock it by hand",
    "share_screen": "open JARVIS Mobile and tap Share screen once; Android asks you each time by design",
    "call": "dial it yourself: 'open the phone's dialer' works without calls permission",
    "send_sms": "'open messages' on the phone and send it there",
    "read_messages": "forward notifications to the PC (Notification access) instead",
    "tap_text": "'read the phone screen' to see what is there, then do it on the phone",
    "read_screen": "turn on the JARVIS control service (Android Accessibility) on the phone",
}


@dataclass
class Row:
    action: str
    declared: bool
    granted: bool
    demonstrated: str                  # "yes" | "never" | "failed" | "stale" | "unsupported"
    verdict: str                       # working | ready | blocked | unsupported | failing | offline | needs_you
    detail: str = ""
    needs: tuple[str, ...] = field(default_factory=tuple)
    proven_at: str = ""


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def build(actions: tuple[str, ...] | list[str], state: dict[str, Any], history: dict[str, tuple[str, str]],
          now: datetime | None = None) -> list[Row]:
    """``state``: the phone's last report (+ ``seen``); ``history``: action -> (latest answer status, its time)."""
    now = now or datetime.now(timezone.utc)
    seen = _parse(state.get("seen"))
    online = seen is not None and now - seen <= STALE_AFTER
    reported = bool(state.get("app_version")) or any(k in state for k in ("control", "comms", "lock"))
    rows: list[Row] = []
    for action in actions:
        needs = NEEDS.get(action, DEFAULT_NEEDS)
        missing = [n for n in needs if reported and not state.get(n)]
        status, at = history.get(action, ("", ""))
        declared = status != "unsupported"
        granted = not missing and reported
        if status == "done":
            when = _parse(at)
            demo = "yes" if when is not None and now - when <= PROOF_VALID else "stale"
        elif status == "unsupported":
            demo = "unsupported"
        elif status == "failed":
            demo = "failed"
        else:
            demo = "never"
        row = Row(action, declared, granted, demo, "ready", needs=needs, proven_at=at if demo in ("yes", "stale") else "")
        if not declared:
            row.verdict, row.detail = "unsupported", "the phone's app is too old for this action; install the current JARVIS Mobile"
        elif not reported:
            row.verdict, row.detail = "offline", "the phone has not reported its settings yet; open JARVIS Mobile on it"
        elif missing:
            row.verdict, row.detail = "blocked", f"off on the phone ({', '.join(missing)}): " + FIX[missing[0]]
        elif not online:
            row.verdict, row.detail = "offline", "the phone has not checked in recently, so this cannot be sent now"
        elif action in CONSENT_EACH_TIME:
            row.verdict, row.detail = "needs_you", "Android asks you to tap Start on the phone each time"
        elif demo == "failed":
            row.verdict, row.detail = "failing", "the last attempt failed on the phone"
        elif demo == "yes":
            row.verdict = "working"
        else:
            row.verdict, row.detail = "ready", "allowed, but not yet proven by a successful run"
        rows.append(row)
    return rows


def explain(row: Row) -> str:
    """One sentence on why not, and what to do instead (empty when the action should work)."""
    if row.verdict in ("working", "ready"):
        return ""
    alt = ALTERNATIVE.get(row.action)
    return f"'{row.action}' is not available: {row.detail}." + (f" Instead: {alt}." if alt else "")


def history_from_db(db: Any, device_id: str) -> dict[str, tuple[str, str]]:
    """Latest finished answer per action from the phone_commands the PC already keeps (no new table)."""
    out: dict[str, tuple[str, str]] = {}
    with db.connect() as conn:
        rows = conn.execute("SELECT action, status, created FROM phone_commands WHERE device_id = ? AND status IN "
                            "('done', 'failed', 'unsupported') ORDER BY id", (device_id,)).fetchall()
    for r in rows:
        out[r["action"]] = (r["status"], r["created"])
    return out


def summary(rows: list[Row]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    order = ("working", "ready", "needs_you", "failing", "blocked", "unsupported", "offline")
    return ", ".join(f"{counts[k]} {k.replace('_', ' ')}" for k in order if k in counts)
