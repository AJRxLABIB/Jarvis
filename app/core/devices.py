"""Device registry, capability detection and Wake-on-LAN - honest about what the hardware can do.

* **This PC** registers itself: name, kind (laptop if it has a battery), its network adapters with MAC
  addresses, and capabilities DETECTED on this PC (never assumed):
    - lock / sleep / restart / shut down: supported (Jarvis's own tools) - hibernate only if `powercfg /a` lists it
    - wake from sleep over the network: "supported" only if an adapter has "wake on magic packet" on
    - wake from full shutdown: at most "unknown" - it also needs the BIOS/UEFI option, a wired adapter and
      Fast startup off, which Windows can't confirm. It becomes "verified" only after a wake really worked.
* **Other devices** (a desktop, a second laptop) are added with their MAC (and address); Jarvis can send them a
  Wake-on-LAN magic packet and then CHECKS: "wake request sent" -> "the device answers on the network" ->
  "Jarvis is ready there". If nothing answers in time it says so - never "it's on".
* A PC that is off can't wake itself, and a phone's web page can't send the packet. To wake THIS laptop from the
  phone you need something on the home network that is on (another PC running Jarvis, a router with a WoL page,
  or a Wake-on-LAN app on the phone) - `wake_setup()` lists exactly what's needed.
"""

from __future__ import annotations

import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from app.tools.base import ToolError
from app.utils.logging_setup import get_logger, log_event

if TYPE_CHECKING:
    from app.memory.database import Database

_log = get_logger("core.devices")
MAC = re.compile(r"^([0-9A-Fa-f]{2})[:-]?([0-9A-Fa-f]{2})[:-]?([0-9A-Fa-f]{2})[:-]?([0-9A-Fa-f]{2})[:-]?"
                 r"([0-9A-Fa-f]{2})[:-]?([0-9A-Fa-f]{2})$")
PROBE_PORTS = (445, 135, 3389, 22, 80)  # "is anything there": file sharing, RPC, remote desktop, ssh, web
WAKE_WAIT_SECONDS = 120


def normalize_mac(text: str) -> str:
    match = MAC.match(text.strip())
    if not match:
        raise ToolError(f"'{text}' isn't a MAC address (like 3C-52-82-1A-2B-3C).", "bad_mac")
    mac = ":".join(g.upper() for g in match.groups())
    if mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
        raise ToolError("That MAC address can't belong to a device.", "bad_mac")
    return mac


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(normalize_mac(mac).replace(":", "")) * 16


def send_magic_packet(mac: str, broadcasts: tuple[str, ...] = ("255.255.255.255",), port: int = 9,
                      repeat: int = 3, sender=None) -> int:
    """Send the wake packet (UDP broadcast). Returns how many packets were sent."""
    packet = magic_packet(mac)
    sent = 0
    if sender is not None:
        for _ in range(repeat):
            for address in broadcasts:
                sender(packet, (address, port))
                sent += 1
        return sent
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeat):
            for address in broadcasts:
                for p in (port, 7):
                    sock.sendto(packet, (address, p))
                    sent += 1
            time.sleep(0.2)
    return sent


def reachable(address: str, ports: tuple[int, ...] = PROBE_PORTS, timeout: float = 0.6) -> int | None:
    """The first TCP port that answers (open OR actively refused both mean the machine is up); None = silent."""
    if not address:
        return None
    for port in ports:
        try:
            with socket.create_connection((address, port), timeout=timeout):
                return port
        except ConnectionRefusedError:
            return port  # something answered: the machine is on
        except OSError:
            continue
    return None


def subnet_broadcast(address: str) -> str | None:
    parts = address.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return ".".join(parts[:3] + ["255"])  # /24, the usual home network
    return None


@dataclass
class Capability:
    name: str
    status: str  # supported | verified | unsupported | unknown
    detail: str = ""


@dataclass
class Device:
    id: str
    name: str
    kind: str
    mac: str
    address: str
    is_self: bool
    last_seen: str | None
    last_state: str
    capabilities: list[Capability] = field(default_factory=list)

    def describe(self) -> str:
        caps = {c.name: c for c in self.capabilities}
        parts = [f"{self.name} ({self.kind})", f"state: {self.last_state}"]
        if self.last_seen:
            parts.append(f"last seen {self.last_seen[:16].replace('T', ' ')}")
        wake = caps.get("wake_from_shutdown") or caps.get("wake_from_sleep")
        if wake:
            parts.append(f"remote wake: {wake.status}" + (f" ({wake.detail})" if wake.detail else ""))
        return ", ".join(parts)


# ------------------------------------------------------------------ this PC: detect, don't assume


def detect_self() -> tuple[dict, list[Capability]]:
    """This PC's identity and capabilities, from Windows itself."""
    import psutil

    from app.tools import winapi

    info = {"name": socket.gethostname()[:60] or "This PC", "kind": "computer", "mac": "", "address": ""}
    try:
        info["kind"] = "laptop" if psutil.sensors_battery() is not None else "desktop"
    except Exception:
        pass
    try:
        from app.remote.server import lan_address

        info["address"] = lan_address()
    except Exception:
        pass
    caps = [Capability(n, "supported", "Jarvis can do it while it's running")
            for n in ("lock", "sleep", "restart", "shutdown")]
    if not winapi.is_windows():
        return info, caps + [Capability("wake_from_sleep", "unknown", "only detectable on Windows")]
    try:
        states = winapi.power_states_text().lower()
        hibernate = "hibernate" in states.split("the following sleep states are not available")[0]
        caps.append(Capability("hibernate", "supported" if hibernate else "unsupported",
                               "" if hibernate else "powercfg /a says hibernate isn't available"))
    except Exception:
        caps.append(Capability("hibernate", "unknown", "couldn't run powercfg /a"))
    adapters = []
    try:
        adapters = winapi.adapter_wake_settings()
    except Exception:
        _log.warning("could not read adapter power settings", exc_info=True)
    wired = [a for a in adapters if "802.3" in str(a.get("Media", "")) or "ethernet" in str(a.get("Desc", "")).lower()]
    magic = [a for a in adapters if str(a.get("Magic", "")).lower() in ("enabled", "2", "true")]
    if adapters:
        info["mac"] = normalize_mac(str((wired or adapters)[0].get("Mac", "")).replace("-", ":")) \
            if (wired or adapters)[0].get("Mac") else ""
    if magic:
        names = ", ".join(str(a.get("Name")) for a in magic)
        caps.append(Capability("wake_from_sleep", "supported", f"magic packet on for {names}"))
    else:
        caps.append(Capability("wake_from_sleep", "unsupported" if adapters else "unknown",
                               "no adapter has 'wake on magic packet' on" if adapters else "no adapter information"))
    fast = winapi.fast_startup_enabled()
    reasons = []
    if not wired:
        reasons.append("no wired (Ethernet) adapter - Wi-Fi usually can't wake a PC that is shut down")
    elif not any(a in magic for a in wired):
        reasons.append("the Ethernet adapter's 'wake on magic packet' is off")
    if fast:
        reasons.append("Fast startup is on (it usually blocks waking from shutdown)")
    reasons.append("the BIOS/UEFI 'Wake on LAN' option can't be checked from Windows")
    status = "unsupported" if not wired else "unknown"
    caps.append(Capability("wake_from_shutdown", status, "; ".join(reasons)))
    return info, caps


def wake_setup(caps: list[Capability], info: dict) -> str:
    """What exactly is needed to wake THIS PC remotely (a checklist, from the detected state)."""
    by = {c.name: c for c in caps}
    lines = []
    sleep = by.get("wake_from_sleep")
    shutdown = by.get("wake_from_shutdown")
    if sleep and sleep.status in ("supported", "verified"):
        lines.append("Waking from sleep: your adapter allows it.")
    else:
        lines.append("Waking from sleep: turn on 'Wake on Magic Packet' (Device Manager -> network adapter -> "
                     "Advanced and Power Management).")
    if shutdown:
        lines.append(f"Waking from shut down: {shutdown.status} - {shutdown.detail}.")
    lines.append(f"Something on your home network must send the packet (this PC can't wake itself): another PC with "
                 f"Jarvis ('wake {info.get('name', 'this PC')}'), your router's Wake-on-LAN page, or a Wake-on-LAN "
                 f"app on your phone using MAC {info.get('mac') or '(unknown)'}"
                 + (f" and address {info['address']}" if info.get("address") else "") + ".")
    lines.append("From outside your home, use a VPN into your network - never open a port for this.")
    return " ".join(lines)


# ------------------------------------------------------------------ the registry


class DeviceRegistry:
    def __init__(self, database: "Database") -> None:
        self._db = database

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def all(self) -> list[Device]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM devices ORDER BY is_self DESC, name").fetchall()
            devices = []
            for r in rows:
                caps = [Capability(c["capability"], c["status"], c["detail"]) for c in conn.execute(
                    "SELECT * FROM device_capabilities WHERE device_id = ? ORDER BY capability", (r["id"],))]
                devices.append(Device(r["id"], r["name"], r["kind"], r["mac"], r["address"], bool(r["is_self"]),
                                      r["last_seen"], r["last_state"], caps))
        return devices

    def find(self, name: str | None) -> Device:
        devices = self.all()
        if not devices:
            raise ToolError("No devices yet. Say 'add a device' with its name and MAC address.", "no_devices")
        key = (name or "").lower().strip()
        own = [d for d in devices if d.is_self]
        if own and key in ("", "this pc", "this computer", "this laptop", "me", "yourself", "you"):
            return own[0]
        for device in devices:
            if device.name.lower() == key:
                return device
        partial = [d for d in devices if key and (key in d.name.lower() or d.name.lower() in key)]
        if len(partial) == 1:
            return partial[0]
        raise ToolError(f"Which device? {', '.join(d.name for d in devices)}.", "ambiguous" if partial else "not_found")

    def register_self(self) -> Device:
        info, caps = detect_self()
        with self._db.transaction() as conn:
            row = conn.execute("SELECT id FROM devices WHERE is_self = 1").fetchone()
            device_id = row["id"] if row else "self"
            if row is None:
                clash = conn.execute("SELECT 1 FROM devices WHERE name = ?", (info["name"],)).fetchone()
                name = info["name"] if clash is None else f"{info['name']} (this PC)"
                conn.execute("INSERT INTO devices (id, name, kind, mac, address, is_self, created_at) "
                             "VALUES (?, ?, ?, ?, ?, 1, ?)", (device_id, name, info["kind"], info["mac"],
                                                              info["address"], self._now()))
            else:
                conn.execute("UPDATE devices SET kind = ?, mac = ?, address = ? WHERE id = ?",
                             (info["kind"], info["mac"], info["address"], device_id))
            conn.execute("UPDATE devices SET last_seen = ?, last_state = 'online' WHERE id = ?", (self._now(), device_id))
            for cap in caps:
                existing = conn.execute("SELECT status FROM device_capabilities WHERE device_id = ? AND capability = ?",
                                        (device_id, cap.name)).fetchone()
                if existing is not None and existing["status"] == "verified":
                    continue  # a wake that really worked stays verified
                conn.execute("INSERT OR REPLACE INTO device_capabilities (device_id, capability, status, detail, "
                             "checked_at) VALUES (?, ?, ?, ?, ?)", (device_id, cap.name, cap.status, cap.detail,
                                                                   self._now()))
        return self.find("this pc")

    def add(self, name: str, mac: str, address: str = "", kind: str = "computer") -> Device:
        name = " ".join(name.split())[:60]
        if not name:
            raise ToolError("What's the device called?", "missing_name")
        mac = normalize_mac(mac)
        if address and not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}|[\w.-]{1,63}", address):
            raise ToolError(f"'{address}' isn't an address.", "bad_address")
        with self._db.transaction() as conn:
            if conn.execute("SELECT 1 FROM devices WHERE lower(name) = lower(?)", (name,)).fetchone():
                raise ToolError(f"There's already a device called {name}.", "exists")
            device_id = secrets.token_hex(6)
            conn.execute("INSERT INTO devices (id, name, kind, mac, address, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (device_id, name, kind[:20], mac, address, self._now()))
            conn.execute("INSERT INTO device_capabilities (device_id, capability, status, detail, checked_at) VALUES "
                         "(?, 'wake_on_lan', 'unknown', 'not tried yet', ?)", (device_id, self._now()))
        return self.find(name)

    def remove(self, device: Device) -> None:
        if device.is_self:
            raise ToolError("This PC can't be removed from its own list.", "self")
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM device_capabilities WHERE device_id = ?", (device.id,))
            conn.execute("DELETE FROM devices WHERE id = ?", (device.id,))

    def set_state(self, device: Device, state: str, seen: bool = False) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE devices SET last_state = ?" + (", last_seen = ?" if seen else "") + " WHERE id = ?",
                         (state, self._now(), device.id) if seen else (state, device.id))

    def set_capability(self, device: Device, capability: str, status: str, detail: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO device_capabilities (device_id, capability, status, detail, checked_at) "
                         "VALUES (?, ?, ?, ?, ?)", (device.id, capability, status, detail[:300], self._now()))


# ------------------------------------------------------------------ waking another device, with verification


@dataclass
class WakeReport:
    sent: int
    stages: list[str]
    online: bool
    jarvis_ready: bool

    def message(self, name: str) -> str:
        return " ".join(self.stages)


def wake(device: Device, registry: DeviceRegistry, *, wait: float = WAKE_WAIT_SECONDS, jarvis_port: int | None = None,
         probe=reachable, sleep=time.sleep, sender=None, clock=time.monotonic) -> WakeReport:
    if device.is_self:
        raise ToolError("I'm running on this PC, so it's already on. To wake it from outside, see 'how can I wake "
                        "this PC remotely'.", "self")
    if not device.mac:
        raise ToolError(f"I don't have {device.name}'s MAC address.", "no_mac")
    broadcasts = tuple(dict.fromkeys(b for b in ("255.255.255.255", subnet_broadcast(device.address)) if b))
    already = probe(device.address) is not None if device.address else False
    if already:
        registry.set_state(device, "online", seen=True)
        return WakeReport(0, [f"{device.name} is already on (it answers on the network)."], True, False)
    sent = send_magic_packet(device.mac, broadcasts, sender=sender)
    stages = [f"Wake request sent to {device.name} ({sent} packets)."]
    log_event(_log, "wake_sent", device=device.id)
    if not device.address:
        stages.append("I can't check whether it woke up: I don't know its network address (add it to the device).")
        return WakeReport(sent, stages, False, False)
    deadline = clock() + wait
    online = False
    while clock() < deadline:
        if probe(device.address) is not None:
            online = True
            break
        sleep(3.0)
    if not online:
        registry.set_state(device, "no answer")
        stages.append(f"{device.name} did not become reachable within {int(wait)} seconds. I couldn't confirm that the "
                      "wake request worked (it may be switched off at the wall, its Wake-on-LAN may be off, or it "
                      "doesn't support waking from this state).")
        return WakeReport(sent, stages, False, False)
    registry.set_state(device, "online", seen=True)
    registry.set_capability(device, "wake_on_lan", "verified", "a wake request brought it online")
    stages.append(f"{device.name} is on: it answers on the network.")
    ready = False
    if jarvis_port:
        for _ in range(20):
            if probe(device.address, (jarvis_port,)) is not None:
                ready = True
                break
            sleep(3.0)
        stages.append("Jarvis is ready there." if ready else "Windows is up, but Jarvis there isn't answering yet.")
    return WakeReport(sent, stages, True, ready)
