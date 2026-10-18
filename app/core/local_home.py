"""Smart-home devices controlled DIRECTLY on your home network - no cloud, no Home Assistant needed.

Supported (all with documented, local HTTP interfaces):
* ``shelly``  - Shelly Gen 1 relays / plugs:  GET /relay/<ch>?turn=on|off, state from GET /relay/<ch> ("ison")
* ``shelly2`` - Shelly Gen 2+ (Plus / Pro):   GET /rpc/Switch.Set?id=<ch>&on=true|false, state Switch.GetStatus
* ``tasmota`` - any Tasmota-flashed plug / switch: GET /cm?cmnd=Power<ch> On|Off, state from cmnd=Power<ch>
* ``wled``    - WLED light strips: POST /json/state {"on": ..., "bri": ...}, state from GET /json/state

Rules:
* Only addresses on your own network (private / link-local IPs, or a ``.local`` name) - never the internet.
* After every change the device is ASKED for its real state: a change is reported as done only when the device says
  so (``verified``); otherwise Jarvis says it couldn't confirm.
* A device marked ``confirm`` (default for relays/plugs - they may switch a heater or a pump) asks before changing.
* Devices without a password only; a device protected by a password is reported as "needs a password" (not stored).

The list lives in config/home_devices.json (edit it, or add devices in Menu > Device manager).
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

KINDS = ("shelly", "shelly2", "tasmota", "wled")
TRANSPORT: httpx.BaseTransport | None = None      # tests replace this
TIMEOUT = 4.0
FILE = "home_devices.json"
_NAME = re.compile(r"^[\w .'-]{1,40}$", re.UNICODE)


class LocalDeviceError(Exception):
    def __init__(self, message: str, code: str = "failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class LocalDevice:
    name: str
    kind: str
    address: str
    channel: int = 0
    confirm: bool = True          # ask before switching it (relays can power anything)

    def describe(self) -> str:
        return f"{self.name} ({self.kind} at {self.address}{'' if not self.channel else f', channel {self.channel}'})"


def check_address(address: str) -> str:
    """Only this network: a private / link-local IP (optionally :port) or a name ending in .local."""
    address = (address or "").strip().lower()
    host, _, port = address.partition(":")
    if port and (not port.isdigit() or not 0 < int(port) < 65536):
        raise LocalDeviceError("That port isn't valid.", "bad_address")
    if host.endswith(".local") and re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.local", host):
        return address
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise LocalDeviceError("Use the device's IP address on your network (e.g. 192.168.1.50) or its name.local.",
                               "bad_address") from exc
    if not (ip.is_private or ip.is_link_local) or ip.is_loopback or ip.is_multicast or ip.is_unspecified:
        raise LocalDeviceError("Only devices on your own network can be added (private addresses).", "not_local")
    return address


def validate(device: LocalDevice) -> LocalDevice:
    if device.kind not in KINDS:
        raise LocalDeviceError(f"Unknown device type '{device.kind}'. Supported: {', '.join(KINDS)}.", "bad_kind")
    if not _NAME.match(device.name or ""):
        raise LocalDeviceError("Give the device a short name (letters, numbers, spaces).", "bad_name")
    device.address = check_address(device.address)
    if not 0 <= int(device.channel) <= 7:
        raise LocalDeviceError("Channel must be 0-7.", "bad_channel")
    return device


class LocalHome:
    def __init__(self, config_dir: Path) -> None:
        self.path = Path(config_dir) / FILE

    # ---- the list
    def devices(self) -> list[LocalDevice]:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for row in rows if isinstance(rows, list) else []:
            try:
                out.append(validate(LocalDevice(str(row["name"]), str(row["kind"]), str(row["address"]),
                                                int(row.get("channel", 0)), bool(row.get("confirm", True)))))
            except (KeyError, TypeError, ValueError, LocalDeviceError):
                continue                                   # a broken entry is skipped, never used half-checked
        return out

    def _save(self, devices: list[LocalDevice]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(d) for d in devices], indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, device: LocalDevice) -> LocalDevice:
        device = validate(device)
        devices = [d for d in self.devices() if d.name.lower() != device.name.lower()]
        devices.append(device)
        self._save(devices)
        return device

    def remove(self, name: str) -> bool:
        devices = self.devices()
        kept = [d for d in devices if d.name.lower() != (name or "").strip().lower()]
        if len(kept) == len(devices):
            return False
        self._save(kept)
        return True

    def find(self, name: str) -> LocalDevice:
        wanted = " ".join((name or "").lower().split())
        devices = self.devices()
        exact = [d for d in devices if d.name.lower() == wanted]
        if exact:
            return exact[0]
        partial = [d for d in devices if wanted and (wanted in d.name.lower() or d.name.lower() in wanted)]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise LocalDeviceError("That could be " + ", ".join(d.name for d in partial) + ". Which one?", "ambiguous")
        known = ", ".join(d.name for d in devices) or "none yet (Menu > Device manager to add one)"
        raise LocalDeviceError(f"I don't know a local device called '{name}'. Known: {known}.", "not_found")


# ---- talking to devices
def _get(url: str) -> Any:
    try:
        with httpx.Client(transport=TRANSPORT, timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise LocalDeviceError("The device didn't answer (is it on and on this network?).", "unreachable") from exc
    return _json(response)


def _post(url: str, payload: dict) -> Any:
    try:
        with httpx.Client(transport=TRANSPORT, timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
            response = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise LocalDeviceError("The device didn't answer (is it on and on this network?).", "unreachable") from exc
    return _json(response)


def _json(response: httpx.Response) -> Any:
    if response.status_code == 401:
        raise LocalDeviceError("The device needs a password; Jarvis only controls devices without one.", "auth")
    if response.status_code >= 400:
        raise LocalDeviceError(f"The device refused (HTTP {response.status_code}).", "device_error")
    try:
        return response.json()
    except ValueError as exc:
        raise LocalDeviceError("The device answered something unexpected (is the type right?).", "bad_reply") from exc


def read_state(device: LocalDevice) -> dict[str, Any]:
    """{'on': bool, 'brightness': 0-255 | None} as the DEVICE reports it."""
    base, ch = f"http://{device.address}", device.channel
    if device.kind == "shelly":
        data = _get(f"{base}/relay/{ch}")
        return {"on": bool(data.get("ison")), "brightness": None}
    if device.kind == "shelly2":
        data = _get(f"{base}/rpc/Switch.GetStatus?id={ch}")
        return {"on": bool(data.get("output")), "brightness": None}
    if device.kind == "tasmota":
        data = _get(f"{base}/cm?cmnd=Power{ch + 1 if ch else ''}")
        value = next((v for k, v in data.items() if k.upper().startswith("POWER")), None) if isinstance(data, dict) else None
        return {"on": str(value).upper() == "ON", "brightness": None}
    data = _get(f"{base}/json/state")                      # wled
    return {"on": bool(data.get("on")), "brightness": data.get("bri")}


def set_state(device: LocalDevice, on: bool | None = None, brightness: int | None = None) -> dict[str, Any]:
    """Change the device, then read it back. Returns {'state': ..., 'verified': bool}."""
    base, ch = f"http://{device.address}", device.channel
    if brightness is not None and device.kind != "wled":
        raise LocalDeviceError(f"{device.name} is a switch: it has no brightness.", "unsupported")
    if device.kind == "shelly":
        _get(f"{base}/relay/{ch}?turn={'on' if on else 'off'}")
    elif device.kind == "shelly2":
        _get(f"{base}/rpc/Switch.Set?id={ch}&on={'true' if on else 'false'}")
    elif device.kind == "tasmota":
        _get(f"{base}/cm?cmnd=Power{ch + 1 if ch else ''}%20{'On' if on else 'Off'}")
    else:
        payload: dict[str, Any] = {}
        if on is not None:
            payload["on"] = bool(on)
        if brightness is not None:
            payload["bri"] = max(1, min(255, int(brightness)))
        _post(f"{base}/json/state", payload)
    state = read_state(device)
    verified = (on is None or state["on"] == bool(on)) and (brightness is None or state.get("brightness") ==
                                                            max(1, min(255, int(brightness))))
    return {"state": state, "verified": verified}
