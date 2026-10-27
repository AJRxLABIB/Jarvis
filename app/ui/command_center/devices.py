"""The unified device panel's data: this PC and every paired phone (and other computers / network devices Jarvis was
told about) with their connection state, what each can REALLY do right now, battery and permissions.

Rules: a metric that wasn't reported is None (shown as "unknown"), never zero; a capability is "available" only when
everything it needs is granted (on the PC AND on the phone); anything else says exactly what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

CONNECTED_SECONDS = 90          # a phone checks in every few seconds while the app is open
RECENT_SECONDS = 3600


@dataclass(frozen=True)
class Capability:
    name: str
    status: str            # available | needs action | unknown
    why: str = ""          # what is missing, or what it needs

    @property
    def glyph(self) -> str:
        return {"available": "✔", "needs action": "⚠"}.get(self.status, "?")


@dataclass
class DeviceCard:
    key: str
    kind: str                                  # pc | phone | computer | network
    name: str
    connection: str                            # connected | recent | offline | pending | local
    connection_text: str
    battery: int | None = None
    charging: bool | None = None
    permissions: list[str] = field(default_factory=list)
    capabilities: list[Capability] = field(default_factory=list)
    activity: list[str] = field(default_factory=list)
    metrics: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def glyph(self) -> str:
        return {"connected": "●", "recent": "◐", "local": "●", "pending": "◌"}.get(self.connection, "○")

    @property
    def headline(self) -> str:
        battery = ""
        if self.battery is not None:
            battery = f" · battery {self.battery}%" + (" (charging)" if self.charging else "")
        return f"{self.glyph} {self.name} - {self.connection_text}{battery}"


def _age(seen: str | None, now: datetime) -> float | None:
    if not seen:
        return None
    try:
        when = datetime.fromisoformat(seen)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds())


def phone_capabilities(permissions: list[str], state: dict) -> list[Capability]:
    """What this phone can do for the PC right now. ``state`` is its last check-in (empty = never checked in)."""
    reported = bool(state)
    granted = set(permissions)

    def on_phone(key: str, label: str, needs: str) -> Capability | None:
        if not reported:
            return Capability(label, "unknown", "the phone hasn't checked in yet")
        return Capability(label, "available") if state.get(key) else Capability(label, "needs action", needs)

    caps: list[Capability] = []
    caps.append(Capability("Status and battery", "available") if "read" in granted
                else Capability("Status and battery", "needs action", "the 'read' permission is off for this phone (PC)"))
    if "control" not in granted:
        control = Capability("Control (flashlight, ring, open apps, lock)", "needs action",
                             "the 'control' permission is off for this phone (PC)")
    else:
        control = on_phone("control", "Control (flashlight, ring, open apps, lock)",
                           "switch on 'Let my PC control this phone' in JARVIS Mobile")
    caps.append(control)
    if "control" in granted:
        caps.append(on_phone("comms", "Calls and messages", "switch on 'Allow calls and messages' in JARVIS Mobile"))
        caps.append(on_phone("lock", "Lock the screen", "activate JARVIS Mobile as a device admin on the phone"))
    caps.append(Capability("Screen view", "available", "ask the phone to start sharing") if "screen" in granted
                else Capability("Screen view", "needs action", "the 'screen' permission is off for this phone (PC)"))
    caps.append(on_phone("notifications", "Forward notifications", "pick apps to forward in JARVIS Mobile"))
    return [c for c in caps if c is not None]


def phone_card(device, state: dict, *, now: datetime | None = None, received: int = 0) -> DeviceCard:
    now = now or datetime.now(timezone.utc)
    age = _age(state.get("seen") or getattr(device, "last_seen", None), now)
    if device.trust == "pending":
        connection, text = "pending", "waiting for you to press Trust"
    elif age is None:
        connection, text = "offline", "never checked in"
    elif age <= CONNECTED_SECONDS:
        connection, text = "connected", "connected"
    elif age <= RECENT_SECONDS:
        connection, text = "recent", f"last seen {int(age // 60)} min ago"
    else:
        connection, text = "offline", "not connected"
    battery = state.get("battery")
    card = DeviceCard(f"phone:{device.id}", "phone", device.name, connection, text,
                      int(battery) if isinstance(battery, (int, float)) and not isinstance(battery, bool) else None,
                      state.get("charging") if isinstance(state.get("charging"), bool) else None,
                      list(device.permissions),
                      phone_capabilities(list(device.permissions), state) if device.trust != "pending" else [])
    if device.trust == "restricted" or set(device.permissions) <= {"read"}:
        card.warnings.append("Restricted: this phone can only read.")
    if state and state.get("app_version") and state["app_version"] != getattr(device, "app_version", state["app_version"]):
        card.warnings.append("The app on the phone changed since pairing.")
    if state:
        card.activity.append(f"last check-in {str(state.get('seen', ''))[:16].replace('T', ' ')} UTC")
    if received:
        card.activity.append(f"{received} file(s) received from the phone")
    if not card.activity:
        card.activity.append("no synchronisation yet")
    return card


def pc_card(name: str, capabilities: list[tuple[str, str]], *, metrics: dict[str, str] | None = None,
            network_mode: str = "", autonomy: str = "") -> DeviceCard:
    caps = []
    for n, s in capabilities:
        status = "available" if s in ("available", "ok", "ready") else (
            "needs action" if s in ("needs_setup", "needs setup", "blocked") else "unknown")
        caps.append(Capability(n.replace("_", " "), status, "" if status == "available" else s))
    return DeviceCard("pc", "pc", name, "local", "this PC" + (f" · {network_mode}" if network_mode else ""),
                      permissions=[{"observe": "autonomy: observe (Jarvis only reads)",
                                    "suggest": "autonomy: suggest (plans, does nothing)",
                                    "approval": "autonomy: approval (every change asks you)",
                                    "routine": "autonomy: routine (low-risk runs, risky asks you)"}.get(autonomy, "")]
                      if autonomy else [],
                      capabilities=caps, metrics=dict(metrics or {}),
                      activity=["running Jarvis now"])


def other_cards(computers: list, local_devices: list) -> list[DeviceCard]:
    cards = []
    for d in computers:
        if getattr(d, "is_self", False):
            continue
        cards.append(DeviceCard(f"computer:{d.name}", "computer", d.name, "offline",
                                f"last known: {getattr(d, 'last_state', 'unknown')}",
                                capabilities=[Capability(c.name.replace("_", " "), "available" if c.status == "available"
                                                         else "unknown", "" if c.status == "available" else c.status)
                                              for c in getattr(d, "capabilities", [])],
                                activity=[f"last seen {(d.last_seen or 'never')[:16].replace('T', ' ')}"]))
    for d in local_devices:
        can = ["on / off"] + (["brightness"] if d.kind == "wled" else [])
        cards.append(DeviceCard(f"network:{d.name}", "network", d.name, "local", f"{d.kind} on your network ({d.address})",
                                capabilities=[Capability(c, "unknown", "press Check to ask the device") for c in can],
                                warnings=["Asks before switching."] if d.confirm else []))
    return cards


def collect(controller, metrics: dict[str, str] | None = None, now: datetime | None = None) -> list[DeviceCard]:
    """All devices, PC first. Every source is optional; a failing one is skipped, never invented."""
    ctx = controller.ctx
    cards: list[DeviceCard] = []
    db = getattr(ctx, "database", None)
    computers: list = []
    if db is not None:
        try:
            from app.core.devices import DeviceRegistry

            computers = DeviceRegistry(db).all()
        except Exception:
            computers = []
    me = next((d for d in computers if getattr(d, "is_self", False)), None)
    mode = getattr(ctx.settings, "offline_mode", "")
    cards.append(pc_card(me.name if me else "This PC", [(c.name, c.status) for c in getattr(me, "capabilities", [])],
                         metrics=metrics, network_mode={"auto": "network mode: automatic", "on": "offline mode on",
                                                        "off": "network allowed"}.get(str(mode), ""),
                         autonomy=str(getattr(ctx.settings, "autonomy", ""))))
    try:
        registry = controller.mobile_registry()
        phones = [d for d in registry.all() if d.trust != "revoked"] if registry is not None else []
    except Exception:
        phones = []
    link = None
    if db is not None and phones:
        from app.remote.phone_link import PhoneLink

        link = PhoneLink(db)
    received = 0
    try:
        from app.remote.phone_inbox import PhoneInbox

        if getattr(ctx, "paths", None) is not None:
            received = len(PhoneInbox(ctx.paths.data_dir).recent(50))
    except Exception:
        received = 0
    for p in phones:
        try:
            state = link.state(p.id) if link is not None else {}
        except Exception:
            state = {}
        cards.append(phone_card(p, state, now=now, received=received if len(phones) == 1 else 0))
    local: list = []
    try:
        if getattr(ctx, "paths", None) is not None:
            from app.core.local_home import LocalHome

            local = LocalHome(ctx.paths.config_dir).devices()
    except Exception:
        local = []
    cards.extend(other_cards(computers, local))
    return cards
