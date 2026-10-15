"""Pairing phones with the phone remote (v2): a one-time code, then a key per phone.

Before 3.4 the key was part of the address (``/?t=<token>``) - it ended up in the phone's browser history,
in screenshots and in anything the link was pasted into. Now:

1. On the PC: Menu -> Phone remote shows the address (no secret in it) and a 6-digit pairing code.
   The code works once, for 5 minutes. Five wrong codes and pairing is locked for 10 minutes.
2. On the phone: open the address, type the code. The PC gives that phone its own random key (256 bits);
   the page keeps it in the phone's local storage and sends it in a header, never in the address.
3. The PC stores only a SHA-256 hash of each key (encrypted for your Windows account with DPAPI), with the
   phone's name, when it was paired and when it was last seen. Each phone can be removed on its own;
   "Forget phone remote pairing" removes them all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEVICES_FILE = "remote_devices.json"
CODE_SECONDS = 300
MAX_WRONG, LOCK_SECONDS = 5, 600
MAX_DEVICES = 8
IDLE_DAYS = 90  # a phone not seen for this long must be paired again (a lost phone stops working by itself)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class Device:
    id: str
    name: str
    key_hash: str
    paired_at: str
    last_seen: str = ""


class PairingStore:
    def __init__(self, data_dir: Path | None, clock=time.time, encrypt: bool = True) -> None:
        self.path = Path(data_dir) / DEVICES_FILE if data_dir is not None else None
        self._clock = clock
        self._encrypt = encrypt
        self._lock = threading.Lock()
        self._code: tuple[str, float] | None = None
        self._wrong: list[float] = []
        self.devices: dict[str, Device] = self._load()

    # -------------------------------------------------------------- storage

    def _read(self) -> bytes:
        assert self.path is not None
        if self._encrypt:
            from app.security.protect import read_bytes

            return read_bytes(self.path)
        return self.path.read_bytes()

    def _load(self) -> dict[str, Device]:
        if self.path is None or not self.path.is_file():
            return {}
        try:
            raw = json.loads(self._read().decode("utf-8"))
            return {d["id"]: Device(**d) for d in raw.get("devices", []) if isinstance(d, dict)}
        except Exception:
            return {}

    def _save(self) -> None:
        if self.path is None:
            return
        data = json.dumps({"devices": [vars(d) for d in self.devices.values()]}).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._encrypt:
            from app.security.protect import write_bytes

            write_bytes(self.path, data)
        else:
            self.path.write_bytes(data)

    # -------------------------------------------------------------- pairing

    def new_code(self) -> str:
        with self._lock:
            code = f"{secrets.randbelow(1_000_000):06d}"
            self._code = (code, self._clock() + CODE_SECONDS)
            return code

    def locked(self) -> bool:
        now = self._clock()
        self._wrong = [t for t in self._wrong if now - t < LOCK_SECONDS]
        return len(self._wrong) >= MAX_WRONG

    def pair(self, code: str, name: str) -> tuple[str, str] | None:
        """(device id, key) for a correct, unexpired code; None otherwise. A code works once."""
        from datetime import datetime

        with self._lock:
            if self.locked():
                return None
            current = self._code
            given = "".join(ch for ch in str(code) if ch.isdigit())[:6]
            if current is None or self._clock() > current[1] or not hmac.compare_digest(given, current[0]):
                self._wrong.append(self._clock())
                return None
            self._code = None  # used
            if len(self.devices) >= MAX_DEVICES:
                oldest = min(self.devices.values(), key=lambda d: d.last_seen or d.paired_at)
                del self.devices[oldest.id]
            device_id, key = secrets.token_hex(8), secrets.token_urlsafe(32)
            clean = " ".join(str(name).split())[:40] or "Phone"
            self.devices[device_id] = Device(device_id, clean, _hash(key),
                                             datetime.now().astimezone().isoformat(timespec="seconds"))
            self._save()
            return device_id, key

    def expired(self, device: Device) -> bool:
        from datetime import datetime, timedelta

        try:
            last = datetime.fromisoformat(device.last_seen or device.paired_at)
        except ValueError:
            return True  # a damaged date must not make a phone live forever
        now = datetime.fromtimestamp(self._clock(), tz=last.tzinfo)
        return now - last > timedelta(days=IDLE_DAYS)

    def check(self, device_id: str, key: str) -> Device | None:
        device = self.devices.get(str(device_id)[:40])
        if device is None or not key or not hmac.compare_digest(_hash(key), device.key_hash):
            return None
        if self.expired(device):
            return None
        return device

    def seen(self, device: Device) -> None:
        from datetime import datetime

        stamp = datetime.now().astimezone().isoformat(timespec="minutes")
        if device.last_seen != stamp:  # at most one write a minute
            device.last_seen = stamp
            try:
                self._save()
            except Exception:
                pass

    def remove(self, device_id: str) -> bool:
        with self._lock:
            if self.devices.pop(device_id, None) is None:
                return False
            self._save()
            return True

    def remove_all(self) -> int:
        with self._lock:
            count = len(self.devices)
            self.devices.clear()
            self._code = None
            self._save()
            return count

    def listing(self) -> list[dict[str, Any]]:
        return [{"id": d.id, "name": d.name, "paired_at": d.paired_at, "last_seen": d.last_seen}
                for d in self.devices.values()]
