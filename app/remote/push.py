"""Push wake-ups for JARVIS Mobile through Firebase Cloud Messaging (PC side).

What this is, and is not:

* **A ping, not a message.** The only thing sent to Google is a data message ``{"kind": "sync"}`` plus the phone's FCM
  token. No notification text, no to-do, no event name, no count. The phone wakes, then asks the PC for details through
  its normal signed, pinned-TLS ``notifications`` command (app/remote/sync.py). Google learns that *a* ping happened.
* **Opt-in twice.** You must (1) put a Firebase service-account file in ``FCM_SERVICE_ACCOUNT`` (your own Firebase project;
  Jarvis ships no Google credentials) and (2) switch the ``notifications`` dataset on for that phone (``mobile_sync``).
  A phone that was never trusted, was revoked, or has the dataset off is never pinged.
* **Bounded.** At most one ping per phone per 30 s, none while Emergency stop is on, tokens Google rejects are deleted, a
  revoked phone's token is deleted with the rest of its sync data.
* **Secrets.** The service-account file is read from where you put it, never copied, logged or put in the database. The
  short-lived Google access token lives in memory only.

Client side: the Android app in this repository is built with plain ``javac`` (no Gradle, no Google Play services), so it
cannot receive FCM. It polls instead (``SyncClient.java``). Receiving push needs a Firebase-enabled build of the app; this
module is the PC half and is tested with a fake Google, never against real Firebase.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.remote.sync import NOTIFICATION_TEXT

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
MIN_INTERVAL = 30.0
TRANSPORT: httpx.BaseTransport | None = None      # tests replace this
_TOKEN = re.compile(r"^[A-Za-z0-9_:\-.]{20,4096}$")
_PROJECT = re.compile(r"^[a-z][a-z0-9\-]{4,29}$")


class PushError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class PushStore:
    def __init__(self, database) -> None:
        self._db = database

    def register(self, device_id: str, token: str) -> None:
        token = (token or "").strip()
        if not _TOKEN.match(token):
            raise PushError("That does not look like an FCM token.")
        with self._db.transaction() as conn:
            latest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            conn.execute("INSERT INTO push_tokens (device_id, token, registered_at, last_event_id) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(device_id) DO UPDATE SET token = excluded.token, registered_at = excluded.registered_at, "
                         "last_error = ''", (device_id, token, _now(), latest))   # old events are never pushed retroactively

    def forget(self, device_id: str) -> bool:
        with self._db.transaction() as conn:
            return conn.execute("DELETE FROM push_tokens WHERE device_id = ?", (device_id,)).rowcount > 0

    def registered(self, device_id: str) -> bool:
        with self._db.connect() as conn:
            return conn.execute("SELECT 1 FROM push_tokens WHERE device_id = ?", (device_id,)).fetchone() is not None

    def targets(self) -> list[dict[str, Any]]:
        """Trusted phones with a token and the notifications dataset switched on."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT t.device_id, t.token, t.last_event_id, t.last_push_at FROM push_tokens t "
                "JOIN mobile_devices d ON d.id = t.device_id AND d.trust = 'trusted' "
                "JOIN sync_prefs p ON p.device_id = t.device_id AND p.dataset = 'notifications' AND p.enabled = 1").fetchall()
        return [dict(r) for r in rows]

    def mark(self, device_id: str, event_id: int | None = None, error: str = "") -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE push_tokens SET last_event_id = COALESCE(?, last_event_id), last_error = ?, "
                         "last_push_at = CASE WHEN ? = '' THEN ? ELSE last_push_at END WHERE device_id = ?",
                         (event_id, error[:200], error, _now(), device_id))


class FcmClient:
    """Google's HTTP v1 API with a service-account key. Blocking; call from a thread."""

    def __init__(self, service_account: Path, clock: Callable[[], float] = time.time) -> None:
        try:
            info = json.loads(Path(service_account).read_text(encoding="utf-8"))
            self._email, self._key, self.project = info["client_email"], info["private_key"], info["project_id"]
        except (OSError, ValueError, KeyError) as exc:
            raise PushError("The FCM service-account file could not be read (it must be the JSON key from Firebase).") from exc
        if not _PROJECT.match(str(self.project)):
            raise PushError("The service-account file has an unexpected project id.")
        self._clock = clock
        self._access: tuple[str, float] | None = None

    def _signed_assertion(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        now = int(self._clock())
        header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64(json.dumps({"iss": self._email, "scope": SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}).encode())
        key = serialization.load_pem_private_key(self._key.encode(), password=None)
        signature = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{claims}.{_b64(signature)}"

    def _http(self) -> httpx.Client:
        return httpx.Client(timeout=15, transport=TRANSPORT, follow_redirects=False)

    def access_token(self) -> str:
        if self._access and self._clock() < self._access[1] - 60:
            return self._access[0]
        with self._http() as client:
            reply = client.post(TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                 "assertion": self._signed_assertion()})
        if reply.status_code != 200:
            raise PushError(f"Google refused the service account (HTTP {reply.status_code}).")
        body = reply.json()
        self._access = (str(body["access_token"]), self._clock() + float(body.get("expires_in", 3600)))
        return self._access[0]

    def ping(self, device_token: str) -> str:
        """Send the content-free wake-up. Returns 'sent', 'gone' (token no longer valid) or raises PushError."""
        message = {"message": {"token": device_token, "data": {"kind": "sync"},
                               "android": {"priority": "high", "ttl": "3600s", "collapse_key": "jarvis-sync"}}}
        with self._http() as client:
            reply = client.post(SEND_URL.format(project=self.project), json=message,
                                headers={"Authorization": f"Bearer {self.access_token()}"})
        if reply.status_code == 200:
            return "sent"
        if reply.status_code in (400, 404) and ("UNREGISTERED" in reply.text or "INVALID_ARGUMENT" in reply.text
                                               or reply.status_code == 404):
            return "gone"
        raise PushError(f"FCM answered HTTP {reply.status_code}.")


def dispatch(database, client: FcmClient, *, emergency_on: bool = False, clock: Callable[[], float] = time.time,
             quiet: Callable[[], bool] = lambda: False) -> dict[str, int]:
    """One pass: ping each eligible phone once if it has new notification-worthy events. Blocking.

    ``quiet()`` True (your quiet hours): only an Emergency event triggers a ping; other events wait and trigger one after
    quiet hours end (unless an Emergency ping already woke the phone, which then reads everything)."""
    out = {"sent": 0, "skipped": 0, "removed": 0, "failed": 0}
    if emergency_on:
        return out
    only = ("Emergency",) if quiet() else None
    store = PushStore(database)
    names = only or tuple(NOTIFICATION_TEXT)
    marks = ",".join("?" for _ in names)
    for target in store.targets():
        with database.connect() as conn:
            newest = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM events WHERE id > ? AND name IN ({marks})",
                                  (target["last_event_id"], *names)).fetchone()[0]
        if not newest:
            continue
        last = target["last_push_at"]
        if last:
            try:
                if clock() - datetime.fromisoformat(last).timestamp() < MIN_INTERVAL:
                    out["skipped"] += 1
                    continue
            except ValueError:
                pass
        try:
            result = client.ping(target["token"])
        except (PushError, httpx.HTTPError) as exc:
            store.mark(target["device_id"], None, f"{exc.__class__.__name__}: {str(exc)[:100]}")
            out["failed"] += 1
            continue
        if result == "gone":
            store.forget(target["device_id"])
            out["removed"] += 1
        else:
            with database.connect() as conn:       # the ping wakes the phone, which then reads everything: move past all
                everything = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM events WHERE name IN ({','.join('?' for _ in NOTIFICATION_TEXT)})",
                                          tuple(NOTIFICATION_TEXT)).fetchone()[0]
            store.mark(target["device_id"], max(newest, everything))
            out["sent"] += 1
    return out
