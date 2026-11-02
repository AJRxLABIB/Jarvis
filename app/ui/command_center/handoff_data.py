"""Rows for the Handoff screen: each package that moves a mission to another device, with its real state and an honest device check."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class Row:
    id: str
    mission: str
    device: str
    state: str                  # issued | accepted | revoked | expired
    left: str
    note: str

    def line(self) -> str:
        return f"{self.mission} -> {self.device}: {self.state.upper()}{' (' + self.left + ' left)' if self.left else ''}"


def _left(expires: str, now: datetime) -> str:
    try:
        end = datetime.fromisoformat(expires)
    except ValueError:
        return ""
    end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    seconds = int((end - now).total_seconds())
    if seconds <= 0:
        return ""
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


def collect(db: Any, *, device_names: dict[str, str] | None = None, mission_titles: dict[str, str] | None = None,
            device_check=None, now: datetime | None = None) -> list[Row]:
    """``device_check(device_id)`` -> a sentence about whether that device can take the work now (or "")."""
    from app.core import handoff

    now = now or datetime.now(timezone.utc)
    rows = []
    for r in handoff.listing(db, now, active_only=False)[:20]:
        note = ""
        if r["effective"] == "issued":
            note = (device_check(r["device_id"]) if device_check else "") or "Valid once, only for that device; it checks its own permissions before continuing."
        elif r["effective"] == "accepted":
            note = "The device took it; continue from the confirmed checkpoint."
        elif r["effective"] == "expired":
            note = "Not used in time. Create a new one if you still want to hand it over."
        rows.append(Row(r["id"], (mission_titles or {}).get(r["mission_id"], r["mission_id"][:8]), (device_names or {}).get(r["device_id"], r["device_id"][:8]),
                        r["effective"], _left(r["expires_at"], now) if r["effective"] == "issued" else "", note))
    return rows
