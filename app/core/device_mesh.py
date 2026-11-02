"""One list of every device JARVIS knows or can see, with trust, availability, abilities, health and what is running on it.

Merges what already exists (nothing is duplicated): the PC and other computers (``core/devices.py``), paired phones with their last check-in and
granted permissions (``remote/mobile.py`` + ``remote/phone_link.py``, abilities from ``ui/command_center/devices.phone_capabilities``), unfinished
handoffs (``core/handoff.py``) and - new - devices seen on the local network (``remote/discovery.py``).

Trust is never created here: a device found on the network is listed as NOT paired until it goes through the existing pairing.
Anything not reported stays "unknown"; nothing is invented. Pure over the database + an injected discovery list, so it is tested without a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Node:
    key: str
    name: str
    kind: str                                   # pc | computer | phone | discovered
    connection: str                             # local | connected | recent | offline | pending | unpaired
    trust: str                                  # this device | trusted | restricted | pending | not paired
    detail: str = ""
    address: str = ""
    battery: int | None = None
    abilities: list[tuple[str, str]] = field(default_factory=list)       # (name, available | needs action | unknown)
    running: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def line(self) -> str:
        battery = f", battery {self.battery}%" if self.battery is not None else ""
        extra = f" [{'; '.join(self.warnings)}]" if self.warnings else ""
        return f"{self.name} ({self.kind}): {self.connection} - {self.detail}{battery}; trust: {self.trust}{extra}"

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "kind": self.kind, "connection": self.connection, "trust": self.trust, "detail": self.detail,
                "address": self.address, "battery": self.battery, "abilities": [{"name": n, "status": s} for n, s in self.abilities],
                "running": self.running, "warnings": self.warnings}


def snapshot(db: Any, found: list | None = None, *, now: datetime | None = None) -> list[Node]:
    """Every known device first (this PC first), then discovered-but-unpaired ones. Each source is optional; a failing one is skipped."""
    from app.ui.command_center import devices as cards

    now = now or datetime.now(timezone.utc)
    nodes: list[Node] = []
    handoffs: dict[str, list[str]] = {}
    try:
        from app.core import handoff

        for h in handoff.listing(db, now):
            handoffs.setdefault(h["device_id"], []).append(f"handoff of mission {h['mission_id'][:8]} waiting (until {h['expires_at'][11:16]} UTC)")
    except Exception:
        pass
    try:
        from app.core.devices import DeviceRegistry

        for d in DeviceRegistry(db).all():
            abilities = [(c.name.replace("_", " "), "available" if c.status in ("available", "verified", "supported") else "unknown") for c in d.capabilities]
            if d.is_self:
                nodes.append(Node("pc", d.name, "pc", "local", "this device", "this PC, running JARVIS now", d.address, abilities=abilities))
            else:
                nodes.append(Node(f"computer:{d.id}", d.name, "computer", "offline", "not paired", f"last known state: {d.last_state}", d.address,
                                  abilities=abilities, warnings=["JARVIS can wake it (Wake-on-LAN) but does not control it"]))
    except Exception:
        pass
    paired: dict[str, str] = {}
    try:
        from app.remote.mobile import MobileRegistry
        from app.remote.phone_link import PhoneLink

        link = PhoneLink(db)
        for m in MobileRegistry(db).all():
            if m.revoked_at:
                continue
            card = cards.phone_card(m, link.state(m.id), now=now)
            paired[m.fingerprint] = m.name
            nodes.append(Node(f"phone:{m.id}", m.name, "phone", card.connection, m.trust, card.connection_text, "", card.battery,
                              [(c.name, c.status) for c in card.capabilities], handoffs.get(m.id, []) + card.activity[:1], card.warnings))
    except Exception:
        pass
    if found:
        from app.remote import discovery

        for match in discovery.classify(found, paired):
            f = match.found
            if match.state == "paired":
                for n in nodes:
                    if n.kind == "phone" and n.name == match.device:
                        n.address = f"{f.address}:{f.port}"
                continue
            nodes.append(Node(f"found:{f.address}:{f.port}", f.txt.get("name", f.instance), "discovered", "unpaired", "not paired",
                              "seen on your network only", f"{f.address}:{f.port}",
                              warnings=["not trusted: pair it (Menu > Mobile devices) before it can do anything"]))
    return nodes


def render(nodes: list[Node]) -> str:
    if not nodes:
        return "No devices known yet."
    return "\n".join(n.line() for n in nodes)
