"""JARVIS Mobile: cryptographic phone identity, pairing with PC-side trust, signed commands, replay protection.

Identity. The phone makes an EC P-256 key pair in the Android Keystore (hardware-backed where the phone has it,
optionally usable only after fingerprint/face/PIN). Only the PUBLIC key is ever sent. The PC stores it with a
fingerprint (SHA-256 of the key), a trust state and permissions.

Pairing.
1. On the PC: "Pair JARVIS Mobile" starts a pairing session: an ID and a one-time secret, valid 5 minutes,
   shown as a QR code together with this PC's TLS certificate fingerprint (so the phone can pin it).
2. The phone sends its public key, name and versions, with an HMAC of them made with the one-time secret
   (proves it saw the QR) and a signature made with its new private key (proves it holds the key).
3. The phone is now PENDING. Nothing works until you press "Trust" on the PC and check the fingerprint shown on
   both screens. Wrong secrets, reused sessions and unknown keys are refused and recorded as security events.

Commands. Every request is an envelope

    {"request_id", "device_id", "timestamp" (ms), "nonce", "command", "arguments" (a JSON *string*), "signature"}

signed over ``request_id \\n device_id \\n timestamp \\n nonce \\n command \\n arguments`` (ECDSA, SHA-256).
The PC checks, in order: well-formed -> known device -> not revoked -> trusted (or restricted) -> timestamp within
CLOCK_WINDOW -> signature -> nonce never seen -> the device has the permission for this command -> the command's
own argument rules. Then the command runs through the SAME executor as a spoken request (permissions,
confirmation, audit); an IP address, a device name or a model's output never authorises anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.utils.logging_setup import get_logger, log_event

if TYPE_CHECKING:
    from app.memory.database import Database

_log = get_logger("remote.mobile")

PAIRING_SECONDS = 300
CLOCK_WINDOW_MS = 120_000
NONCE_KEEP_SECONDS = 600
MAX_ARGUMENT_CHARS = 8000
TRUST_STATES = ("pending", "trusted", "restricted", "revoked")
# What a phone may be allowed to do. "emergency" can't be taken away from a trusted phone.
PERMISSIONS = ("read", "chat", "approve", "control", "power", "screen", "files", "admin")
DEFAULT_PERMISSIONS = ("read", "chat", "approve")  # the minimum; control / power are switched on by you, on the PC
RESTRICTED_PERMISSIONS = ("read",)
LABELS = {"read": "see status, notifications and messages", "chat": "talk to Jarvis",
          "approve": "answer Jarvis's approval questions", "control": "volume, media, open apps",
          "power": "lock, sleep, hibernate, restart, shut down, wake other PCs",
          "screen": "see and control the screen (mouse and keyboard) in an active session",
          "files": "browse and transfer files", "admin": "system settings (not yet available)"}

# command -> (permission needed, tool it becomes (or None = handled here), argument rules)
COMMANDS: dict[str, tuple[str | None, str | None]] = {
    "hello": ("read", None),          # "am I paired, what's my trust state"
    "status": ("read", None),
    "events": ("read", None),         # new messages + the approval question waiting
    "chat": ("chat", None),
    "approve": ("approve", None),
    "emergency_stop": (None, None),   # always allowed for a trusted or restricted phone
    "lock": ("power", "power_control"),
    "sleep": ("power", "power_control"),
    "hibernate": ("power", "power_control"),
    "restart": ("power", "power_control"),
    "shutdown": ("power", "power_control"),
    "wake_device": ("power", "wake_device"),
    "volume": ("control", "volume_control"),
    "media": ("control", "media_control"),
    "open_app": ("control", "open_application"),
    "close_app": ("control", "close_application"),
    "window": ("control", "window_control"),
    "brightness": ("control", "brightness_control"),
    "open_website": ("control", "open_website"),
    "browser": ("control", None),          # open/read/click/type/back/tabs via the browser tools
    "capabilities": ("read", None),        # what this PC can do + Windows auth options
    "wake_config": ("read", None),         # this PC's Wake-on-LAN details (MAC, broadcast) for the app
    "auth_options": ("read", None),        # supported Windows sign-in methods when locked
    "missions": ("read", None),            # mission_status, read-only
    "list_files": ("files", None),
    "read_file": ("files", None),
    "get_file": ("files", None),
    "put_file": ("files", None),
    "screen": ("screen", None),            # a still frame (an active, unlocked session only)
    "mouse": ("screen", None),
    "key": ("screen", None),
    "register_auth_key": (None, None),     # enrol the biometric key (signed by the base key)
}

# Commands that change something on the PC: the phone must sign them with its SECOND key, the one Android
# releases only after fingerprint / face / device PIN. Read-only commands need only the base key.
STEP_UP_COMMANDS = frozenset({
    "lock", "sleep", "hibernate", "restart", "shutdown", "wake_device", "volume", "media", "open_app", "close_app",
    "window", "brightness", "open_website", "browser", "put_file", "get_file", "mouse", "key",
})
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class MobileError(Exception):
    """A refused request. ``status`` is the HTTP status the phone gets."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_public_key(der_b64: str):
    """A P-256 public key from base64 SubjectPublicKeyInfo DER (what Android's PublicKey.getEncoded() gives)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    try:
        key = load_der_public_key(base64.b64decode(der_b64, validate=True))
    except Exception as exc:
        raise MobileError(400, "bad_key", "That isn't a valid public key.") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise MobileError(400, "bad_key", "Only P-256 keys are accepted.")
    return key


def fingerprint(der_b64: str) -> str:
    """SHA-256 of the public key, first 16 bytes as AB:CD:... (shown on the PC and the phone to compare)."""
    digest = hashlib.sha256(base64.b64decode(der_b64)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, 32, 2))


def verify_signature(key, message: bytes, signature_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        key.verify(base64.b64decode(signature_b64, validate=True), message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def signed_text(envelope: dict[str, Any]) -> bytes:
    return "\n".join(str(envelope.get(k, "")) for k in
                     ("request_id", "device_id", "timestamp", "nonce", "command", "arguments")).encode("utf-8")


def pairing_text(pid: str, public_key: str, name: str) -> bytes:
    return f"{pid}\n{public_key}\n{name}".encode("utf-8")


@dataclass
class PairingSession:
    id: str
    secret: str
    expires: float
    used: bool = False


@dataclass
class MobileDevice:
    id: str
    name: str
    platform: str
    os_version: str
    app_version: str
    public_key: str
    fingerprint: str
    key_info: dict[str, Any]
    trust: str
    permissions: list[str]
    paired_at: str
    trusted_at: str | None = None
    last_seen: str | None = None
    revoked_at: str | None = None
    auth_public_key: str = ""
    auth_fingerprint: str = ""
    _key: Any = field(default=None, repr=False, compare=False)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "platform": self.platform, "os_version": self.os_version,
                "app_version": self.app_version, "fingerprint": self.fingerprint, "trust": self.trust,
                "permissions": list(self.permissions), "paired_at": self.paired_at, "trusted_at": self.trusted_at,
                "last_seen": self.last_seen, "revoked_at": self.revoked_at,
                "key_hardware": bool(self.key_info.get("hardware")),
                "key_needs_biometric": bool(self.key_info.get("user_auth")),
                "auth_key": bool(self.auth_public_key), "auth_fingerprint": self.auth_fingerprint}


class MobileRegistry:
    """The phones this PC knows, their trust and permissions (database), pairing sessions (memory)."""

    COLUMNS = ("id, name, platform, os_version, app_version, public_key, fingerprint, key_info, trust, permissions, "
               "paired_at, trusted_at, last_seen, revoked_at, auth_public_key, auth_fingerprint")

    def __init__(self, database: "Database", clock=time.time) -> None:
        self._db, self._clock = database, clock
        self._lock = threading.Lock()
        self._session: PairingSession | None = None
        self.on_pending = None  # callback(MobileDevice): the window asks "trust this phone?"
        self.on_security_event = None  # callback(kind, detail)

    # ------------------------------------------------------------ devices

    def _row(self, row) -> MobileDevice:
        return MobileDevice(row["id"], row["name"], row["platform"], row["os_version"], row["app_version"],
                            row["public_key"], row["fingerprint"], json.loads(row["key_info"] or "{}"), row["trust"],
                            json.loads(row["permissions"] or "[]"), row["paired_at"], row["trusted_at"],
                            row["last_seen"], row["revoked_at"],
                            row["auth_public_key"] if "auth_public_key" in row.keys() else "",
                            row["auth_fingerprint"] if "auth_fingerprint" in row.keys() else "")

    def get(self, device_id: str) -> MobileDevice | None:
        with self._db.connect() as conn:
            row = conn.execute(f"SELECT {self.COLUMNS} FROM mobile_devices WHERE id = ?", (device_id,)).fetchone()
        return self._row(row) if row else None

    def all(self) -> list[MobileDevice]:
        with self._db.connect() as conn:
            rows = conn.execute(f"SELECT {self.COLUMNS} FROM mobile_devices ORDER BY paired_at DESC").fetchall()
        return [self._row(r) for r in rows]

    def set_trust(self, device_id: str, trust: str) -> MobileDevice:
        """From the window only (a person at the PC)."""
        if trust not in TRUST_STATES or trust == "pending":
            raise ValueError(trust)
        device = self.get(device_id)
        if device is None:
            raise KeyError(device_id)
        if device.trust == "revoked" and trust != "revoked":
            raise ValueError("a revoked phone must pair again")
        now = _now_iso()
        permissions = device.permissions
        if trust == "restricted":
            permissions = [p for p in permissions if p in RESTRICTED_PERMISSIONS]
        with self._db.transaction() as conn:
            conn.execute("UPDATE mobile_devices SET trust = ?, permissions = ?, trusted_at = COALESCE(?, trusted_at), "
                         "revoked_at = ? WHERE id = ?",
                         (trust, json.dumps(permissions), now if trust == "trusted" else None,
                          now if trust == "revoked" else None, device_id))
        self.security_event(device_id, f"trust_{trust}", device.name)
        return self.get(device_id)  # type: ignore[return-value]

    def set_permissions(self, device_id: str, permissions: list[str]) -> MobileDevice:
        """From the window only. A restricted phone keeps read-only."""
        device = self.get(device_id)
        if device is None:
            raise KeyError(device_id)
        chosen = [p for p in PERMISSIONS if p in set(permissions)]
        if device.trust == "restricted":
            chosen = [p for p in chosen if p in RESTRICTED_PERMISSIONS]
        with self._db.transaction() as conn:
            conn.execute("UPDATE mobile_devices SET permissions = ? WHERE id = ?", (json.dumps(chosen), device_id))
        self.security_event(device_id, "permissions", ",".join(chosen))
        return self.get(device_id)  # type: ignore[return-value]

    def rename(self, device_id: str, name: str) -> None:
        clean = " ".join(str(name).split())[:40]
        if clean:
            with self._db.transaction() as conn:
                conn.execute("UPDATE mobile_devices SET name = ? WHERE id = ?", (clean, device_id))

    def seen(self, device: MobileDevice) -> None:
        stamp = _now_iso()[:16]
        if (device.last_seen or "")[:16] != stamp:
            device.last_seen = _now_iso()
            with self._db.transaction() as conn:
                conn.execute("UPDATE mobile_devices SET last_seen = ? WHERE id = ?", (device.last_seen, device.id))

    def security_event(self, device_id: str | None, kind: str, detail: str = "") -> None:
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO mobile_security_events (at, device_id, kind, detail) VALUES (?, ?, ?, ?)",
                         (_now_iso(), device_id, kind[:40], str(detail)[:300]))
            conn.execute("DELETE FROM mobile_security_events WHERE id NOT IN (SELECT id FROM mobile_security_events "
                         "ORDER BY id DESC LIMIT 500)")
        log_event(_log, "mobile_security", kind=kind, device=device_id)
        if self.on_security_event is not None and kind in ("pairing_refused", "bad_signature", "revoked_used",
                                                           "replay", "unknown_device"):
            try:
                self.on_security_event(kind, detail)
            except Exception:
                _log.warning("security alert callback failed", exc_info=True)

    def security_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT at, device_id, kind, detail FROM mobile_security_events ORDER BY id DESC "
                                "LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ pairing

    def start_pairing(self) -> PairingSession:
        with self._lock:
            self._session = PairingSession(secrets.token_hex(8), secrets.token_urlsafe(24),
                                           self._clock() + PAIRING_SECONDS)
            return self._session

    def pairing_left(self) -> int:
        session = self._session
        if session is None or session.used:
            return 0
        return max(0, int(session.expires - self._clock()))

    def pair(self, request: dict[str, Any]) -> MobileDevice:
        """A phone's pairing request -> a PENDING device. Raises MobileError (and records why) otherwise."""
        pid = str(request.get("pairing_id", ""))[:40]
        public_key = str(request.get("public_key", ""))[:400]
        name = " ".join(str(request.get("name", "")).split())[:40] or "Android phone"
        with self._lock:
            session = self._session
            valid = (session is not None and not session.used and self._clock() <= session.expires
                     and hmac.compare_digest(pid, session.id))
            if not valid:
                self.security_event(None, "pairing_refused", f"{name}: no valid pairing session")
                raise MobileError(403, "pairing_expired", "This pairing code has expired or was already used. "
                                  "Start pairing again on the PC.")
            expected = base64.b64encode(hmac.new(session.secret.encode(), pairing_text(pid, public_key, name),
                                                 hashlib.sha256).digest()).decode()
            if not hmac.compare_digest(expected, str(request.get("proof", ""))):
                self.security_event(None, "pairing_refused", f"{name}: wrong pairing secret")
                raise MobileError(403, "bad_proof", "The pairing code doesn't match.")
            key = load_public_key(public_key)
            if not verify_signature(key, pairing_text(pid, public_key, name), str(request.get("signature", ""))):
                self.security_event(None, "pairing_refused", f"{name}: key possession not proven")
                raise MobileError(403, "bad_signature", "The phone couldn't prove it holds its key.")
            session.used = True  # one phone per pairing session
        key_print = fingerprint(public_key)
        info = request.get("key_info") if isinstance(request.get("key_info"), dict) else {}
        key_info = {"hardware": bool(info.get("hardware")), "user_auth": bool(info.get("user_auth")),
                    "reported_by": "app (not attested)"}
        device_id = secrets.token_hex(8)
        with self._db.transaction() as conn:
            clash = conn.execute("SELECT id, trust FROM mobile_devices WHERE fingerprint = ?", (key_print,)).fetchone()
            if clash is not None:
                raise MobileError(409, "already_paired", "This phone's key is already paired"
                                  + (" and was revoked: make a new key on the phone." if clash["trust"] == "revoked"
                                     else "."))
            conn.execute("INSERT INTO mobile_devices (id, name, platform, os_version, app_version, public_key, "
                         "fingerprint, key_info, trust, permissions, paired_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                         "'pending', ?, ?)",
                         (device_id, name, str(request.get("platform", "android"))[:20],
                          str(request.get("os_version", ""))[:40], str(request.get("app_version", ""))[:20],
                          public_key, key_print, json.dumps(key_info), json.dumps(list(DEFAULT_PERMISSIONS)),
                          _now_iso()))
        device = self.get(device_id)
        self.security_event(device_id, "paired_pending", name)
        if self.on_pending is not None:
            try:
                self.on_pending(device)
            except Exception:
                _log.warning("pending-device callback failed", exc_info=True)
        return device  # type: ignore[return-value]

    def register_auth_key(self, device: MobileDevice, auth_public_key: str, signature: str) -> str:
        """Enrol the phone's SECOND key - the one Android releases only after fingerprint / face / PIN, used to
        sign every action. The request is already signed by the BASE key (checked in verify), and here the new key
        signs its own bytes too (proof the phone holds it). Set once; changing it means pairing again."""
        auth_public_key = str(auth_public_key)[:400]
        new_key = load_public_key(auth_public_key)
        proof = "\n".join(("auth-key", device.id, auth_public_key)).encode("utf-8")
        if not verify_signature(new_key, proof, str(signature)):
            raise MobileError(403, "bad_signature", "The protected key couldn't prove itself.")
        if device.auth_public_key and device.auth_public_key != auth_public_key:
            raise MobileError(409, "auth_key_set", "A protected key is already set for this phone. To replace it, "
                              "pair the phone again.")
        print_ = fingerprint(auth_public_key)
        with self._db.transaction() as conn:
            conn.execute("UPDATE mobile_devices SET auth_public_key = ?, auth_fingerprint = ? WHERE id = ?",
                         (auth_public_key, print_, device.id))
        self.security_event(device.id, "auth_key_registered", print_[:20])
        return print_

    # ------------------------------------------------------------ commands

    def verify(self, envelope: Any) -> tuple[MobileDevice, str, dict[str, Any]]:
        """-> (device, command, arguments) or MobileError. Nothing runs before every check passed."""
        if not isinstance(envelope, dict):
            raise MobileError(400, "malformed", "Not a command.")
        for name in ("request_id", "device_id", "nonce", "command", "arguments", "signature"):
            if not isinstance(envelope.get(name), str):
                raise MobileError(400, "malformed", f"'{name}' is missing.")
        if not _ID.match(envelope["request_id"]) or not _ID.match(envelope["device_id"]) \
                or not _NONCE.match(envelope["nonce"]):
            raise MobileError(400, "malformed", "Bad request, device or nonce ID.")
        timestamp = envelope.get("timestamp")
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            raise MobileError(400, "malformed", "'timestamp' must be milliseconds.")
        command = envelope["command"]
        if command not in COMMANDS:
            raise MobileError(400, "unknown_command", f"Unknown command '{command[:40]}'.")
        if len(envelope["arguments"]) > MAX_ARGUMENT_CHARS:
            raise MobileError(413, "too_big", "The arguments are too long.")
        device = self.get(envelope["device_id"])
        if device is None:
            self.security_event(None, "unknown_device", envelope["device_id"])
            raise MobileError(401, "unknown_device", "This phone isn't paired with this PC.")
        if device.trust == "revoked":
            self.security_event(device.id, "revoked_used", command)
            raise MobileError(403, "revoked", "This phone was removed from Jarvis. Pair it again on the PC.")
        now_ms = int(self._clock() * 1000)
        if abs(now_ms - timestamp) > CLOCK_WINDOW_MS:
            raise MobileError(408, "expired", "The request is too old (or the phone's clock is wrong).")
        step_up = command in STEP_UP_COMMANDS
        if step_up:
            if not device.auth_public_key:
                raise MobileError(403, "no_auth_key", "This action needs the phone's protected key. Open JARVIS "
                                  "Mobile and finish setup (it makes a fingerprint/PIN-protected key).")
            verifying_key = load_public_key(device.auth_public_key)  # released on the phone only after biometrics
        else:
            verifying_key = load_public_key(device.public_key)
        if not verify_signature(verifying_key, signed_text(envelope), envelope["signature"]):
            self.security_event(device.id, "bad_signature", command)
            raise MobileError(401, "bad_signature", "The signature doesn't match this phone's key.")
        self._use_nonce(device.id, envelope["nonce"])
        if device.trust == "pending" and command != "hello":
            raise MobileError(403, "pending", "Waiting for you to trust this phone on the PC.")
        permission = COMMANDS[command][0]
        if permission is not None and permission not in device.permissions:
            self.security_event(device.id, "permission_denied", command)
            raise MobileError(403, "not_permitted", f"This phone isn't allowed to '{LABELS.get(permission, permission)}'."
                              " Change it on the PC: Menu -> Mobile devices.")
        try:
            arguments = json.loads(envelope["arguments"] or "{}")
        except ValueError as exc:
            raise MobileError(400, "malformed", "The arguments aren't JSON.") from exc
        if not isinstance(arguments, dict):
            raise MobileError(400, "malformed", "The arguments must be an object.")
        self.seen(device)
        return device, command, arguments

    def _use_nonce(self, device_id: str, nonce: str) -> None:
        now = self._clock()
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM mobile_nonces WHERE seen_at < ?", (now - NONCE_KEEP_SECONDS,))
            try:
                conn.execute("INSERT INTO mobile_nonces (device_id, nonce, seen_at) VALUES (?, ?, ?)",
                             (device_id, nonce, now))
            except Exception as exc:  # the primary key says: seen before
                if "UNIQUE" not in str(exc).upper() and "PRIMARY" not in str(exc).upper():
                    raise
                replay = True
            else:
                replay = False
        if replay:
            self.security_event(device_id, "replay", nonce[:12])
            raise MobileError(409, "replay", "This request was already used.")


_POWER_ACTIONS = {"lock": "lock", "sleep": "sleep", "hibernate": "hibernate", "restart": "restart",
                  "shutdown": "shutdown"}


def tool_call_for(command: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The executor call a control/power command becomes. Only these fixed shapes - nothing free-form."""
    if command in _POWER_ACTIONS:
        return "power_control", {"action": _POWER_ACTIONS[command]}
    if command == "wake_device":
        name = str(arguments.get("name", ""))[:60].strip()
        if not name:
            raise MobileError(400, "malformed", "Which device?")
        return "wake_device", {"name": name}
    if command == "volume":
        action = arguments.get("action")
        if action not in ("up", "down", "mute", "unmute", "set", "get"):
            raise MobileError(400, "malformed", "volume action must be up/down/mute/unmute/set/get")
        call: dict[str, Any] = {"action": action}
        if action == "set":
            level = arguments.get("level")
            if not isinstance(level, int) or not 0 <= level <= 100:
                raise MobileError(400, "malformed", "volume level must be 0-100")
            call["level"] = level
        return "volume_control", call
    if command == "media":
        action = arguments.get("action")
        if action not in ("play_pause", "next", "previous", "stop"):
            raise MobileError(400, "malformed", "media action must be play_pause/next/previous/stop")
        return "media_control", {"action": action}
    if command == "open_app":
        name = str(arguments.get("name", ""))[:64].strip()
        if not name:
            raise MobileError(400, "malformed", "Which app?")
        return "open_application", {"name": name}
    if command == "close_app":
        name = str(arguments.get("name", ""))[:64].strip()
        if not name:
            raise MobileError(400, "malformed", "Which app?")
        return "close_application", {"name": name}
    if command == "window":
        action = arguments.get("action")
        if action not in ("focus", "minimize", "maximize", "restore", "minimize_all"):
            raise MobileError(400, "malformed", "window action must be focus/minimize/maximize/restore/minimize_all")
        call = {"action": action}
        if action != "minimize_all":
            target = str(arguments.get("app", ""))[:64].strip()
            if not target:
                raise MobileError(400, "malformed", "Which window?")
            call["app"] = target
        return "window_control", call
    if command == "brightness":
        action = arguments.get("action")
        if action not in ("up", "down", "set", "get"):
            raise MobileError(400, "malformed", "brightness action must be up/down/set/get")
        call = {"action": action}
        if action == "set":
            level = arguments.get("level")
            if not isinstance(level, int) or not 0 <= level <= 100:
                raise MobileError(400, "malformed", "brightness level must be 0-100")
            call["level"] = level
        return "brightness_control", call
    if command == "open_website":
        site = str(arguments.get("site", ""))[:200].strip()
        if not site:
            raise MobileError(400, "malformed", "Which website?")
        return "open_website", {"site": site}
    raise MobileError(400, "unknown_command", command)


def browser_call_for(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The 'browser' phone command -> one of the existing browser_* tools (its own safety rules still apply)."""
    action = arguments.get("action")
    if action == "open":
        url = str(arguments.get("url", ""))[:500].strip()
        if not url:
            raise MobileError(400, "malformed", "browser open needs 'url'")
        return "browser_open", {"url": url}
    if action == "read":
        return "browser_read", {}
    if action == "back":
        return "browser_back", {}
    if action == "click":
        target = arguments.get("target")
        if isinstance(target, int) and not isinstance(target, bool):
            return "browser_click", {"element": target}
        if isinstance(target, str) and target.strip():
            return "browser_click", {"text": target[:100]}
        raise MobileError(400, "malformed", "browser click needs 'target' (a number or link text)")
    if action == "type":
        text = str(arguments.get("text", ""))
        if not text:
            raise MobileError(400, "malformed", "browser type needs 'text'")
        call: dict[str, Any] = {"text": text[:2000]}
        field = arguments.get("field")
        if isinstance(field, str) and field.strip():
            call["field"] = field[:100]
        if arguments.get("submit") is True:
            call["submit"] = True
        return "browser_type", call
    if action == "tabs":
        tab_action = str(arguments.get("tabs_action", "list"))
        if tab_action not in ("list", "switch", "new", "close"):
            raise MobileError(400, "malformed", "tabs action must be list/switch/new/close")
        call = {"action": tab_action}
        index = arguments.get("index")
        if isinstance(index, int) and not isinstance(index, bool):
            call["index"] = index
        return "browser_tabs", call
    raise MobileError(400, "malformed", "browser action must be open/read/click/type/back/tabs")


# ------------------------------------------------------------------ remote sessions (audit + terminate)


@dataclass
class MobileSession:
    id: str
    device_id: str
    started_at: str
    last_activity: str
    scopes: list[str]
    network: str
    ended_at: str | None = None
    end_reason: str = ""

    @property
    def active(self) -> bool:
        return self.ended_at is None

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "device_id": self.device_id, "started_at": self.started_at,
                "last_activity": self.last_activity, "scopes": list(self.scopes), "network": self.network,
                "ended_at": self.ended_at, "end_reason": self.end_reason, "active": self.active}


class SessionStore:
    """Remote-control sessions: one per phone, opened when it starts controlling, closed on stop, timeout,
    revoke or emergency stop. Kept for the audit trail and so you can end one from the PC."""

    IDLE_SECONDS = 900

    def __init__(self, database: "Database", clock=time.time) -> None:
        self._db, self._clock = database, clock

    def _iso(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(timespec="seconds")

    def _row(self, row) -> MobileSession:
        return MobileSession(row["id"], row["device_id"], row["started_at"], row["last_activity"],
                             json.loads(row["scopes"] or "[]"), row["network"], row["ended_at"], row["end_reason"])

    def open(self, device_id: str, scopes: list[str], network: str = "") -> MobileSession:
        """The device's current live session (reused if one is open), touched to now."""
        existing = self.active_for(device_id)
        now = self._iso()
        if existing is not None:
            with self._db.transaction() as conn:
                conn.execute("UPDATE mobile_sessions SET last_activity = ?, scopes = ? WHERE id = ?",
                             (now, json.dumps(sorted(set(existing.scopes) | set(scopes))), existing.id))
            return self.get(existing.id)  # type: ignore[return-value]
        session_id = secrets.token_hex(8)
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO mobile_sessions (id, device_id, started_at, last_activity, scopes, network) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (session_id, device_id, now, now, json.dumps(sorted(set(scopes))), network[:60]))
        return self.get(session_id)  # type: ignore[return-value]

    def touch(self, session_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE mobile_sessions SET last_activity = ? WHERE id = ? AND ended_at IS NULL",
                         (self._iso(), session_id))

    def get(self, session_id: str) -> MobileSession | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM mobile_sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row(row) if row else None

    def active_for(self, device_id: str) -> MobileSession | None:
        self.expire_idle()
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM mobile_sessions WHERE device_id = ? AND ended_at IS NULL "
                               "ORDER BY started_at DESC LIMIT 1", (device_id,)).fetchone()
        return self._row(row) if row else None

    def active(self) -> list[MobileSession]:
        self.expire_idle()
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM mobile_sessions WHERE ended_at IS NULL ORDER BY started_at DESC")
            return [self._row(r) for r in rows.fetchall()]

    def recent(self, limit: int = 30) -> list[MobileSession]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM mobile_sessions ORDER BY started_at DESC LIMIT ?", (limit,))
            return [self._row(r) for r in rows.fetchall()]

    def end(self, session_id: str, reason: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE mobile_sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
                         (self._iso(), reason[:60], session_id))

    def end_device(self, device_id: str, reason: str) -> int:
        with self._db.transaction() as conn:
            return conn.execute("UPDATE mobile_sessions SET ended_at = ?, end_reason = ? WHERE device_id = ? AND "
                                "ended_at IS NULL", (self._iso(), reason[:60], device_id)).rowcount

    def end_all(self, reason: str) -> int:
        with self._db.transaction() as conn:
            return conn.execute("UPDATE mobile_sessions SET ended_at = ?, end_reason = ? WHERE ended_at IS NULL",
                                (self._iso(), reason[:60])).rowcount

    def expire_idle(self) -> int:
        cutoff = datetime.fromtimestamp(self._clock() - self.IDLE_SECONDS, timezone.utc).isoformat(timespec="seconds")
        with self._db.transaction() as conn:
            return conn.execute("UPDATE mobile_sessions SET ended_at = ?, end_reason = 'timed out' WHERE "
                                "ended_at IS NULL AND last_activity < ?", (self._iso(), cutoff)).rowcount
