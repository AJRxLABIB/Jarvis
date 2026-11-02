"""Phone remote (REMOTE_ENABLED): talk to Jarvis from your phone's browser on your home network.

* Only devices on your private network (192.168.x.x, 10.x, 172.16-31.x) are answered.
* Pairing (v2, app/remote/pairing.py): Menu -> Phone remote shows the address and a one-time 6-digit code;
  the phone gets its own key, sent in a header (never in the address). Every request except the page
  itself and the pairing step needs a paired phone's key. "Forget phone remote pairing" removes all phones.
* Answers to questions ("allow this?") name the exact question they answer; a stale answer is refused.
* Everything you send goes through the same checks as typing on the PC: permissions,
  confirmations (you can answer them on the phone), audit log.
* Plain HTTP on the local network (no internet exposure). Windows asks once whether to allow
  Jarvis through the firewall on private networks.
"""

from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path
from urllib.parse import urlencode
import socket
import time
from collections import defaultdict, deque
from typing import Any, Callable

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("remote")
RATE_LIMIT, RATE_WINDOW = 40, 10.0  # requests per window per address


def _security(message: str, priority: str) -> None:
    from app.core import events

    try:
        events.BUS.publish(events.SECURITY_EVENT, message=message, priority=priority)
    except Exception:
        pass


def lan_address() -> str:
    """This PC's address on the home network (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.168.1.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


WAKE_APK = Path(__file__).resolve().parent / "assets" / "JarvisWake.apk"  # legacy name; see app/remote/apk.py
TAILSCALE_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def is_private(address: str, *, tailscale: bool = False) -> bool:
    """Home network (and, when allowed, your own Tailscale network - only devices you signed in reach it)."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if tailscale and any(ip in network for network in TAILSCALE_NETWORKS if ip.version == network.version):
        return True
    return ip.is_private or ip.is_loopback


def tailscale_address() -> str | None:
    """This PC's Tailscale address (100.x.y.z), if Tailscale is installed and connected."""
    try:
        import psutil

        for addresses in psutil.net_if_addrs().values():
            for address in addresses:
                if address.family == socket.AF_INET and ipaddress.ip_address(address.address) in TAILSCALE_NETWORKS[0]:
                    return address.address
    except Exception:
        return None
    return None


class RemoteHub:
    """What the web page sees: the conversation, a pending question, and a way to send text."""

    def __init__(self, send: Callable[[str], None], answer: Callable[[bool, str], bool],
                 pending: Callable[[], dict | None], status: Callable[[], dict],
                 voice: Callable[[bytes], str] | None = None,
                 wake_config: Callable[[], dict] | None = None) -> None:
        self.wake_config = wake_config  # this PC's details for the Jarvis Wake phone app
        self.mobile = None  # app.remote.mobile.MobileRegistry: JARVIS Mobile phones (signed requests)
        self.sessions = None  # app.remote.mobile.SessionStore: remote-control sessions (audit + terminate)
        self.ops_ctx = None  # ToolContext: for files / screen / input handlers (app/remote/mobile_ops.py)
        self.pc_capabilities = None  # callable() -> dict: what this PC can do + Windows sign-in options
        self.pc_wake_config = None  # callable() -> dict: this PC's Wake-on-LAN details for the app
        self.emergency: Callable[[], str] | None = None  # emergency stop (always allowed for a trusted phone)
        self.cancel: Callable[[], str] | None = None     # cancel just the current request (phone permission "chat")
        self.run_tool = None  # async (tool, arguments) -> dict: through the executor (the safety kernel)
        self.phone_screen = None  # app.remote.phone_screen.PhoneScreenHub: a phone sharing its screen with this PC
        self.phone_inbox = None   # app.remote.phone_inbox.PhoneInbox: files a phone sends to this PC
        self.on_file = None       # callable(phone name, path): tell the window a file arrived
        self.clipboard = None     # callable(text): put text from the phone onto the PC's clipboard (window thread)
        self.send, self.answer, self.pending, self.status = send, answer, pending, status
        self.voice = voice  # audio from the phone -> the text it heard (then sent like a message)
        self.messages: deque[dict[str, Any]] = deque(maxlen=200)
        self._next = 1

    def add(self, role: str, text: str) -> None:
        if not text:
            return
        self.messages.append({"id": self._next, "role": role, "text": text[:4000], "time": time.strftime("%H:%M")})
        self._next += 1

    def since(self, last: int) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["id"] > last]


MAX_VOICE_BYTES = 2 * 1024 * 1024  # about a minute of phone audio
# polling / reading commands are answered fresh every time; everything else is remembered by request_id (idempotency)
READ_ONLY_PHONE_COMMANDS = frozenset({"hello", "status", "events", "wait_events", "capabilities", "auth_options",
                                      "wake_config", "missions", "phone_commands", "screen_frame"})
FRAME_RATE_LIMIT = 60              # screen frames per RATE_WINDOW per address (the phone sends about 3 a second)
WAIT_MAX = 25.0                    # seconds a phone's "wait_events" request is held open at most


class RemoteServer:
    def __init__(self, hub: RemoteHub, pairing, port: int, host: str = "0.0.0.0", ssl_context=None,
                 allow_tailscale: bool = False) -> None:
        self.hub, self.pairing, self.port, self.host = hub, pairing, port, host
        self.allow_tailscale = allow_tailscale
        self.tls_fingerprint = ""  # SHA-256 of this PC's certificate: JARVIS Mobile pins it when it pairs
        self.ssl_context = ssl_context  # REMOTE_HTTPS: needed for the phone's microphone and notifications
        self._runner = None
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    @property
    def url(self) -> str:
        scheme = "https" if self.ssl_context is not None else "http"
        return f"{scheme}://{lan_address()}:{self.port}/"

    @property
    def away_url(self) -> str | None:
        """The address for away from home (Tailscale), when it's allowed and connected."""
        if not self.allow_tailscale:
            return None
        address = tailscale_address()
        scheme = "https" if self.ssl_context is not None else "http"
        return f"{scheme}://{address}:{self.port}/" if address else None

    @staticmethod
    def _network_path(address: str) -> str:
        """Which way the phone reached us (for the session record): LAN, Tailscale or loopback."""
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return address[:40]
        if any(ip in net for net in TAILSCALE_NETWORKS if ip.version == net.version):
            return "Tailscale"
        if ip.is_loopback:
            return "loopback"
        return f"LAN {ip}"

    def _answered(self, device_id: str, request_id: str) -> dict | None:
        from app.remote.mobile import IDEMPOTENT_SECONDS

        now = time.monotonic()
        answers = self.__dict__.setdefault("_answers", {})
        for key in [k for k, (at, _) in answers.items() if now - at > IDEMPOTENT_SECONDS]:
            del answers[key]
        found = answers.get((device_id, request_id))
        return found[1] if found else None

    def _remember_answer(self, device_id: str, request_id: str, answer: dict) -> None:
        answers = self.__dict__.setdefault("_answers", {})
        answers[(device_id, request_id)] = (time.monotonic(), answer)
        while len(answers) > 500:
            answers.pop(next(iter(answers)))

    def _reachable(self, request, bucket: str = "", limit: int = RATE_LIMIT) -> tuple[bool, int]:
        """Private network and not too many requests (every endpoint, even the page). Screen frames count in their
        own bucket (``bucket``), so sharing the phone's screen never starves the normal requests."""
        address = request.remote or ""
        if not is_private(address, tailscale=self.allow_tailscale):
            return False, 403
        hits = self._hits[bucket + address]
        now = time.monotonic()
        while hits and now - hits[0] > RATE_WINDOW:
            hits.popleft()
        if len(hits) >= limit:
            return False, 429
        hits.append(now)
        return True, 200

    def _allowed(self, request) -> tuple[bool, int]:
        from app.core import emergency

        if emergency.STATE.flags.get("remote_disabled"):
            return False, 503  # emergency controls: the phone remote is off
        ok, code = self._reachable(request)
        if not ok:
            return ok, code
        device = self.pairing.check(request.headers.get("X-Device", ""), request.headers.get("X-Key", ""))
        if device is None:
            return False, 401
        self.pairing.seen(device)
        return True, 200

    async def _wait_events(self, arguments: dict, step: float = 0.25) -> dict:
        """Long poll: answer as soon as there is a new message or the approval question changed, else after
        ``timeout`` seconds (max WAIT_MAX). The phone keeps one such request open while the app is open, so updates
        arrive at once without Firebase/Google services. Read-only, signed like every other command."""
        import asyncio

        since = arguments.get("since", 0)
        since = since if isinstance(since, int) and since >= 0 else 0
        known = str(arguments.get("pending_id", "") or "")[:40]
        timeout = arguments.get("timeout", WAIT_MAX)
        timeout = max(1.0, min(float(timeout) if isinstance(timeout, (int, float)) else WAIT_MAX, WAIT_MAX))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        hub = self.hub
        while True:
            messages, pending = hub.since(since), hub.pending()
            changed = str((pending or {}).get("id", "") or "") != known
            if messages or changed or loop.time() >= deadline:
                return {"ok": True, "messages": messages, "pending": pending, "changed": bool(messages or changed)}
            await asyncio.sleep(step)

    async def _mobile_run(self, device, command: str, arguments: dict) -> dict:
        """A verified, permitted JARVIS Mobile command. Tools go through the executor like a spoken request."""
        import asyncio

        from app.remote import mobile_ops
        from app.remote.mobile import STEP_UP_COMMANDS, MobileError, browser_call_for, tool_call_for

        hub = self.hub
        if command == "hello":
            return {"ok": True, "trust": device.trust, "permissions": device.permissions, "name": device.name,
                    "auth_key": bool(device.auth_public_key), "auth_fingerprint": device.auth_fingerprint}
        if command == "register_auth_key":
            if hub.mobile is None:
                raise MobileError(503, "unavailable", "unavailable")
            print_ = hub.mobile.register_auth_key(device, arguments.get("auth_public_key", ""),
                                                  arguments.get("signature", ""))
            return {"ok": True, "auth_fingerprint": print_}
        if command == "status":
            return {"ok": True, "status": hub.status()}
        if command == "capabilities":
            caps = hub.pc_capabilities() if hub.pc_capabilities else {}
            return {"ok": True, **caps}
        if command == "auth_options":
            caps = hub.pc_capabilities() if hub.pc_capabilities else {}
            return {"ok": True, "auth": caps.get("auth", {})}
        if command == "wake_config":
            cfg = hub.pc_wake_config() if hub.pc_wake_config else {}
            if not cfg.get("mac"):
                raise MobileError(404, "no_mac", cfg.get("error") or "This PC's wired MAC isn't known yet.")
            return {"ok": True, "mac": cfg.get("mac", ""), "broadcast": cfg.get("broadcast", ""),
                    "ip": cfg.get("ip", ""), "port": cfg.get("port", ""), "name": cfg.get("name", ""),
                    "public": cfg.get("public", ""), "public_port": cfg.get("public_port", ""),
                    "away": cfg.get("away", ""), "wake_status": cfg.get("wake_status", "")}
        if command == "events":
            since = arguments.get("since", 0)
            return {"ok": True, "messages": hub.since(since if isinstance(since, int) else 0),
                    "pending": hub.pending()}
        if command == "wait_events":
            return await self._wait_events(arguments)
        if command == "phone_clipboard":
            text = str(arguments.get("text", ""))[:8000]
            if not text.strip():
                raise MobileError(400, "malformed", "The clipboard was empty.")
            if hub.clipboard is not None:
                hub.clipboard(text)
            return {"ok": True, "message": "On your PC's clipboard."}
        if command == "chat":
            text = str(arguments.get("text", "")).strip()[:2000]
            if not text:
                raise MobileError(400, "malformed", "Say something.")
            hub.send(text)
            return {"ok": True}
        if command == "approve":
            approved, request_id = arguments.get("approved"), str(arguments.get("id", ""))[:40]
            if not isinstance(approved, bool) or not request_id:
                raise MobileError(400, "malformed", "approve needs id and approved (true/false)")
            return {"ok": bool(hub.answer(approved, request_id))}
        if command == "cancel":
            if hub.cancel is None:
                raise MobileError(503, "unavailable", "Cancel isn't connected.")
            return {"ok": True, "message": hub.cancel()}
        if command == "emergency_stop":
            if hub.sessions is not None:
                hub.sessions.end_device(device.id, "emergency stop")
            if hub.emergency is None:
                raise MobileError(503, "unavailable", "Emergency stop isn't connected.")
            return {"ok": True, "message": hub.emergency()}
        if command == "missions":
            if hub.run_tool is None:
                raise MobileError(503, "unavailable", "unavailable")
            return await hub.run_tool("mission_status", {})

        if command in ("phone_commands", "phone_result", "phone_notify", "phone_share"):
            if hub.mobile is None:
                raise MobileError(503, "unavailable", "unavailable")
            from app.remote.phone_link import PhoneLink, redact_codes

            link = PhoneLink(hub.mobile.database)

            def _phone() -> dict:
                if command == "phone_commands":
                    return {"ok": True, "commands": link.collect(device.id, arguments.get("report") or {})}
                if command == "phone_result":
                    raw = arguments.get("id")
                    if not isinstance(raw, int) or isinstance(raw, bool):
                        raise MobileError(400, "bad_request", "id")
                    return {"ok": link.report(device.id, raw, bool(arguments.get("ok")), str(arguments.get("message", "")),
                                              unsupported=bool(arguments.get("unsupported")))}
                from app.core.notifications import NotificationCenter

                center = NotificationCenter(hub.mobile.database)
                if command == "phone_notify":
                    app_name = redact_codes(str(arguments.get("app", "")))[:40] or "Phone"
                    title = redact_codes(str(arguments.get("title", "")))[:150]
                    text = redact_codes(str(arguments.get("text", "")))[:500]
                    from app.core import notification_intel

                    verdict = notification_intel.CLASSIFIER.classify(app_name, title, text)
                    if verdict.duplicate or verdict.label == notification_intel.PROMO:      # repeats and promotions are not worth the centre's attention
                        return {"ok": True, "filtered": verdict.label if not verdict.duplicate else "duplicate"}
                    hint = notification_intel.reminder_hint(verdict)
                    body = (text + (f"\n{hint}" if hint else ""))[:1000]
                    why = f"\n({verdict.explain()})" if verdict.label == notification_intel.SUSPICIOUS else ""
                    center.add(f"{verdict.tag()}{app_name}: {title}" if title else f"{verdict.tag()}{app_name}", body + why, source=f"phone:{device.name}"[:40],
                               category="security" if verdict.label in (notification_intel.CRITICAL, notification_intel.SUSPICIOUS) else "phone",
                               priority=notification_intel.PRIORITY[verdict.label])
                    return {"ok": True, "label": verdict.label}
                text = redact_codes(str(arguments.get("text", "")))[:2000].strip()
                if not text:
                    raise MobileError(400, "bad_request", "text")
                center.add("Shared from your phone", text, source=f"phone:{device.name}"[:40], category="phone")
                return {"ok": True, "message": "Sent to your PC."}

            return await asyncio.to_thread(_phone)

        if command in ("sync_status", "sync_pull", "sync_push", "sync_forget", "notifications", "push_register"):
            if hub.mobile is None:
                raise MobileError(503, "unavailable", "unavailable")
            from app.remote.sync import SyncError, SyncStore

            store = SyncStore(hub.mobile.database)

            def _int(name: str, default: int) -> int:
                value = arguments.get(name, default)
                return value if isinstance(value, int) and not isinstance(value, bool) else default

            def _sync() -> dict:
                dataset = str(arguments.get("dataset", "todos"))[:20]
                if command == "push_register":
                    from app.remote.push import PushError, PushStore

                    try:
                        PushStore(hub.mobile.database).register(device.id, str(arguments.get("token", "")))
                    except PushError as exc:
                        raise SyncError(400, "bad_token", str(exc)) from exc
                    return {"ok": True, "registered": True,
                            "note": "pings are only sent when FCM is set up on the PC and notifications sync is on"}
                if command == "sync_status":
                    from app.remote.push import PushStore

                    return {"ok": True, "datasets": store.status(device.id),
                            "push": "registered" if PushStore(hub.mobile.database).registered(device.id) else "not registered (polling)"}
                if command == "sync_forget":
                    from app.remote.push import PushStore

                    PushStore(hub.mobile.database).forget(device.id)
                    return {"ok": True, "removed": store.forget_device(device.id)}
                if command == "notifications":
                    return {"ok": True, **store.notifications(device.id, _int("since", 0), _int("limit", 30))}
                if command == "sync_pull":
                    return {"ok": True, **store.pull(device.id, dataset, _int("since", 0), _int("limit", 200))}
                return {"ok": True, **store.push(device.id, dataset, arguments.get("changes"))}

            try:
                return await asyncio.to_thread(_sync)
            except SyncError as exc:
                raise MobileError(exc.status, exc.code, str(exc)) from exc

        # everything below touches files, the screen or the system: record/refresh a control session
        if command in STEP_UP_COMMANDS or command in ("list_files", "read_file", "get_file", "screen"):
            if hub.sessions is not None:
                hub.sessions.open(device.id, [command], network=getattr(hub, "_last_network", ""))

        if command in ("list_files", "read_file", "get_file", "put_file", "screen", "mouse", "key"):
            if hub.ops_ctx is None:
                raise MobileError(503, "unavailable", "The PC handlers aren't ready.")
            handler = {"list_files": mobile_ops.list_files, "read_file": mobile_ops.read_file,
                       "get_file": mobile_ops.get_file, "put_file": mobile_ops.put_file,
                       "screen": mobile_ops.screen_frame, "mouse": mobile_ops.mouse, "key": mobile_ops.key}[command]
            return await asyncio.to_thread(handler, hub.ops_ctx, arguments)

        if hub.run_tool is None:
            raise MobileError(503, "unavailable", "Jarvis can't run actions right now.")
        if command == "browser":
            tool, tool_arguments = browser_call_for(arguments)
        else:
            tool, tool_arguments = tool_call_for(command, arguments)
        return await hub.run_tool(tool, tool_arguments)

    def app(self):
        from aiohttp import web

        def guard(handler):
            async def wrapped(request):
                ok, code = self._allowed(request)
                if not ok:
                    log_event(_log, "remote_refused", code=code)
                    return web.Response(status=code, text="Not allowed")
                return await handler(request)
            return wrapped

        def open_guard(handler):  # the page and pairing: no key yet, but still private-network + rate limit
            async def wrapped(request):
                ok, code = self._reachable(request)
                if not ok:
                    log_event(_log, "remote_refused", code=code)
                    return web.Response(status=code, text="Not allowed")
                return await handler(request)
            return wrapped

        async def pair(request):
            if request.content_length and request.content_length > 2048:
                return web.json_response({"ok": False}, status=413)
            try:
                data = await request.json()
            except Exception:
                return web.json_response({"ok": False}, status=400)
            if self.pairing.locked():
                return web.json_response({"ok": False, "error": "Too many wrong codes. Wait 10 minutes."}, status=429)
            result = self.pairing.pair(str(data.get("code", "")), str(data.get("name", "Phone")))
            if result is None:
                log_event(_log, "remote_pair_refused")
                _security("A phone tried to pair with a wrong or expired code.", "normal")
                return web.json_response({"ok": False, "error": "That code is wrong or expired. Get a new one on "
                                          "the PC: Menu -> Phone remote."}, status=401)
            log_event(_log, "remote_paired")
            _security(f"A new phone was paired with the phone remote ({str(data.get('name', 'Phone'))[:40]}).", "high")
            return web.json_response({"ok": True, "device": result[0], "key": result[1]})

        async def page(request):
            return web.Response(text=PAGE, content_type="text/html",
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                         "Content-Security-Policy": "default-src 'self' 'unsafe-inline'"})

        async def messages(request):
            last = int(request.query.get("since", "0") or 0)
            return web.json_response({"messages": self.hub.since(last), "pending": self.hub.pending(),
                                      "status": self.hub.status()})

        async def send(request):
            if request.content_length and request.content_length > 64 * 1024:
                return web.json_response({"ok": False}, status=413)
            data = await request.json()
            text = str(data.get("text", "")).strip()[:2000]
            if not text:
                return web.json_response({"ok": False}, status=400)
            self.hub.send(text)
            log_event(_log, "remote_message")
            return web.json_response({"ok": True})

        async def voice(request):
            if self.hub.voice is None:
                return web.json_response({"ok": False, "error": "voice is not available"}, status=503)
            audio = await request.read()
            if not audio or len(audio) > MAX_VOICE_BYTES:
                return web.json_response({"ok": False, "error": "no audio, or too long"}, status=400)
            import asyncio

            try:
                text = await asyncio.to_thread(self.hub.voice, audio)
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)[:200]}, status=500)
            log_event(_log, "remote_voice", heard=bool(text))
            if text:
                self.hub.send(text)
            return web.json_response({"ok": bool(text), "text": text})

        async def confirm(request):
            data = await request.json()
            request_id = str(data.get("id", ""))[:40]
            return web.json_response({"ok": self.hub.answer(bool(data.get("approved")), request_id)})

        application = web.Application(client_max_size=MAX_VOICE_BYTES + 1024)
        async def wake_app(request):  # the JARVIS Mobile app (wake + control in one); home network / Tailscale only
            from app.remote import apk

            path = apk.apk_path()
            if path is None:
                return web.Response(status=404, text="The JARVIS Mobile app isn't built into this Jarvis yet. On the "
                                    "PC run packaging\\build_android.ps1, then rebuild Jarvis.")
            log_event(_log, "mobile_apk_downloaded")
            return web.FileResponse(path, headers={
                "Content-Type": "application/vnd.android.package-archive",
                "Content-Disposition": f'attachment; filename="{path.name}"', "Cache-Control": "no-store"})

        async def mobile_app_info(request):  # version + SHA-256 so the phone/desktop can verify the APK
            from app.remote import apk

            return web.json_response({"ok": True, **apk.info()})

        async def mobile_apk(request):  # the JARVIS Mobile APK (home network / Tailscale only, like the page)
            from app.remote import apk

            path = apk.apk_path()
            if path is None:
                return web.Response(status=404, text=apk.info()["message"])
            log_event(_log, "mobile_apk_downloaded")
            return web.FileResponse(path, headers={
                "Content-Type": "application/vnd.android.package-archive",
                "Content-Disposition": f'attachment; filename="{path.name}"', "Cache-Control": "no-store"})

        async def wake_config(request):  # paired phones only: the details the app needs to wake this PC
            config = self.hub.wake_config() if self.hub.wake_config else {}
            if not config.get("mac"):
                return web.json_response({"ok": False, "error": config.get("error") or "This PC's wired MAC address "
                                          "isn't known yet."}, status=404)
            query = urlencode({k: v for k, v in config.items() if v not in (None, "") and k != "notes"})
            return web.json_response({"ok": True, "link": f"jarviswake://setup?{query}", **config,
                                      "app_available": WAKE_APK.is_file()})

        async def mobile_pair(request):  # JARVIS Mobile: public key + proof of the QR's one-time secret
            from app.remote.mobile import MobileError

            if self.hub.mobile is None:
                return web.json_response({"ok": False, "error": "unavailable"}, status=503)
            if (request.content_length or 0) > 16 * 1024:
                return web.json_response({"ok": False, "error": "too_big"}, status=413)
            try:
                device = self.hub.mobile.pair(await request.json())
            except MobileError as exc:
                return web.json_response({"ok": False, "error": exc.code, "message": str(exc)}, status=exc.status)
            except ValueError:
                return web.json_response({"ok": False, "error": "malformed"}, status=400)
            return web.json_response({"ok": True, "device_id": device.id, "fingerprint": device.fingerprint,
                                      "trust": device.trust, "message": "Now press Trust on the PC after checking "
                                      "the fingerprint is the same on both screens."})

        async def mobile_pair_page(request):  # the QR opens this; it hands the details to the JARVIS Mobile app
            import html as _html

            pid, secret = request.query.get("pid", "")[:40], request.query.get("s", "")[:80]
            host, _, port = request.host.rpartition(":")
            query = urlencode({"host": host or request.host, "port": port or str(self.port), "pid": pid, "s": secret,
                               "fp": self.tls_fingerprint, "pc": socket.gethostname()[:40]})
            link = _html.escape(f"jarviswake://pair?{query}", quote=True)
            body = (f'<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>Pair JARVIS Mobile</title><body style="background:#02050b;color:#d6e6f5;font:16px '
                    f'system-ui;padding:24px"><h2 style="color:#38bdf8">Pair JARVIS Mobile</h2>'
                    f'<p>Open this in the JARVIS Mobile app (install it first from the PC\'s QR code).</p>'
                    f'<p><a href="{link}" style="display:inline-block;padding:14px 20px;background:#38bdf8;'
                    f'color:#02050b;border-radius:10px;text-decoration:none"><b>Open JARVIS Mobile</b></a></p>'
                    f'<p style="color:#6f8ba6">The code works once and expires 5 minutes after it was shown.</p>')
            return web.Response(text=body, content_type="text/html", headers={
                "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

        async def mobile_command(request):  # every JARVIS Mobile request: signed, fresh, permitted
            from app.remote.mobile import MobileError

            if self.hub.mobile is None:
                return web.json_response({"ok": False, "error": "unavailable"}, status=503)
            if (request.content_length or 0) > 16 * 1024:
                return web.json_response({"ok": False, "error": "too_big"}, status=413)
            try:
                envelope = await request.json()
            except ValueError:
                return web.json_response({"ok": False, "error": "malformed"}, status=400)
            self.hub._last_network = self._network_path(request.remote or "")
            try:
                device, command, arguments = self.hub.mobile.verify(envelope)
                earlier = self._answered(device.id, envelope["request_id"])
                if earlier is not None:             # the same request again (its answer was lost): never run twice
                    log_event(_log, "mobile_duplicate", command=command, device=device.id)
                    return web.json_response({**earlier, "duplicate": True})
                result = await self._mobile_run(device, command, arguments)
                if command not in READ_ONLY_PHONE_COMMANDS:
                    self._remember_answer(device.id, envelope["request_id"], {"request_id": envelope["request_id"], **result})
            except MobileError as exc:
                log_event(_log, "mobile_refused", code=exc.code)
                return web.json_response({"ok": False, "error": exc.code, "message": str(exc)}, status=exc.status)
            log_event(_log, "mobile_command", command=command, device=device.id, ok=result.get("ok"))
            return web.json_response({"request_id": envelope.get("request_id"), **result})

        async def mobile_frame(request):  # JARVIS Mobile "Share screen": one signed JPEG frame (app/remote/phone_screen.py)
            from app.core import emergency
            from app.remote.mobile import MobileError
            from app.remote.phone_screen import MAX_FRAME_BYTES, ScreenError

            ok, code = self._reachable(request, bucket="frame:", limit=FRAME_RATE_LIMIT)
            if not ok:
                return web.json_response({"ok": False, "error": "rate"}, status=code)
            if self.hub.mobile is None or self.hub.phone_screen is None:
                return web.json_response({"ok": False, "error": "unavailable"}, status=503)
            if (request.content_length or 0) > MAX_FRAME_BYTES * 4 // 3 + 4096:
                return web.json_response({"ok": False, "error": "too_big"}, status=413)
            try:
                envelope = await request.json()
            except ValueError:
                return web.json_response({"ok": False, "error": "malformed"}, status=400)
            try:
                device, command, arguments = self.hub.mobile.verify(envelope)
                if command not in ("screen_frame", "screen_stopped"):
                    raise MobileError(400, "malformed", "This address only takes screen frames.")
                screens = self.hub.phone_screen
                if command == "screen_stopped" or emergency.STATE.flags.get("remote_disabled"):
                    result = screens.stopped(device)
                    result["stop"] = True
                else:
                    result = screens.frame(device, arguments)
            except MobileError as exc:
                return web.json_response({"ok": False, "error": exc.code, "message": str(exc)}, status=exc.status)
            except ScreenError as exc:
                return web.json_response({"ok": False, "error": exc.code, "message": str(exc)}, status=400)
            return web.json_response({"request_id": envelope.get("request_id"), **result})

        async def mobile_upload(request):  # Share > JARVIS Mobile: one signed file (app/remote/phone_inbox.py)
            from app.remote.mobile import MobileError
            from app.remote.phone_inbox import MAX_FILE_BYTES, InboxError

            ok, code = self._reachable(request, bucket="upload:", limit=20)
            if not ok:
                return web.json_response({"ok": False, "error": "rate"}, status=code)
            if self.hub.mobile is None or self.hub.phone_inbox is None:
                return web.json_response({"ok": False, "error": "unavailable"}, status=503)
            limit = MAX_FILE_BYTES * 4 // 3 + 16 * 1024
            if (request.content_length or 0) > limit:
                return web.json_response({"ok": False, "error": "too_big"}, status=413)
            body = bytearray()
            while True:                     # read it ourselves: the server-wide body limit stays small
                chunk = await request.content.readany()
                if not chunk:
                    break
                body += chunk
                if len(body) > limit:
                    return web.json_response({"ok": False, "error": "too_big"}, status=413)
            try:
                import json as _json

                envelope = _json.loads(bytes(body))
            except ValueError:
                return web.json_response({"ok": False, "error": "malformed"}, status=400)
            try:
                device, command, arguments = self.hub.mobile.verify(envelope)
                if command != "phone_file":
                    raise MobileError(400, "malformed", "This address only takes files.")
                path = await asyncio.to_thread(self.hub.phone_inbox.save, str(arguments.get("name", "")),
                                               arguments.get("data", ""))
            except MobileError as exc:
                return web.json_response({"ok": False, "error": exc.code, "message": str(exc)}, status=exc.status)
            except InboxError as exc:
                return web.json_response({"ok": False, "error": exc.code, "message": str(exc)}, status=400)
            log_event(_log, "phone_file_received", device=device.id, bytes=path.stat().st_size)
            if self.hub.on_file is not None:
                self.hub.on_file(device.name, path)
            return web.json_response({"request_id": envelope.get("request_id"), "ok": True,
                                      "message": f"Saved on your PC as {path.name}."})

        async def manifest(request):  # "Add to Home screen": opens like an app, full screen
            return web.json_response({"name": "Jarvis", "short_name": "Jarvis", "start_url": "/", "display": "standalone",
                                      "background_color": "#02050b", "theme_color": "#02050b",
                                      "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})

        async def icon(request):
            return web.Response(text=ICON, content_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

        application.router.add_get("/", open_guard(page))
        application.router.add_get("/manifest.json", open_guard(manifest))
        application.router.add_get("/wake-app.apk", open_guard(wake_app))
        application.router.add_get("/mobile/app.apk", open_guard(mobile_apk))
        application.router.add_get("/api/mobile/app-info", open_guard(mobile_app_info))
        application.router.add_get("/mobile/pair", open_guard(mobile_pair_page))
        application.router.add_post("/api/mobile/pair", open_guard(mobile_pair))
        application.router.add_post("/api/mobile/command", open_guard(mobile_command))
        application.router.add_post("/api/mobile/frame", mobile_frame)
        application.router.add_post("/api/mobile/upload", mobile_upload)
        application.router.add_get("/api/wake-config", guard(wake_config))
        application.router.add_get("/icon.svg", open_guard(icon))
        application.router.add_post("/api/pair", open_guard(pair))
        application.router.add_get("/api/messages", guard(messages))
        application.router.add_post("/api/send", guard(send))
        application.router.add_post("/api/confirm", guard(confirm))
        application.router.add_post("/api/voice", guard(voice))
        return application

    async def start(self) -> None:
        from aiohttp import web

        self._runner = web.AppRunner(self.app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port, ssl_context=self.ssl_context).start()
        log_event(_log, "remote_started", port=self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><rect width="96" height="96" rx="20" fill="#02050b"/>'
        '<circle cx="48" cy="48" r="30" fill="none" stroke="#38bdf8" stroke-width="6"/>'
        '<circle cx="48" cy="48" r="12" fill="#e0f7ff"/></svg>')

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jarvis</title>
<link rel="manifest" href="/manifest.json"><link rel="icon" href="/icon.svg"><link rel="apple-touch-icon" href="/icon.svg">
<meta name="theme-color" content="#02050b"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<style>
:root{--bg:#02050b;--panel:#07121f;--edge:#153a57;--cyan:#38bdf8;--gold:#f5b642;--text:#d6e6f5;--dim:#6f8ba6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,Segoe UI,sans-serif;
display:flex;flex-direction:column;height:100vh}
header{padding:12px 16px;border-bottom:1px solid var(--edge);display:flex;align-items:center;gap:10px}
.orb{width:22px;height:22px;border-radius:50%;background:radial-gradient(circle,#fff,var(--cyan) 45%,#0b3a55 75%);
box-shadow:0 0 14px var(--cyan)}h1{font-size:16px;letter-spacing:5px;margin:0}#state{margin-left:auto;color:var(--dim);font-size:12px}
#log{flex:1;overflow:auto;padding:12px 16px}.m{margin:8px 0;max-width:88%;padding:8px 12px;border-radius:12px;background:var(--panel);
border:1px solid var(--edge);white-space:pre-wrap;word-wrap:break-word}.user{margin-left:auto;border-color:var(--cyan)}
.assistant .who{color:var(--gold)}.notice{color:#fcd98a;font-size:13px}.who{font-size:11px;letter-spacing:2px;color:var(--cyan)}
#ask{display:none;margin:8px 16px;padding:10px;border:1px solid var(--gold);border-radius:12px}
#ask button{margin:6px 6px 0 0}.chips{display:flex;gap:6px;padding:0 16px 8px;overflow:auto}
button{background:#0b2438;color:var(--text);border:1px solid var(--edge);border-radius:18px;padding:8px 14px;font:inherit;white-space:nowrap}
form{display:flex;gap:8px;padding:10px 16px 16px;border-top:1px solid var(--edge)}
input{flex:1;background:#040c18;color:var(--text);border:1px solid var(--edge);border-radius:20px;padding:10px 14px;font:inherit}
.send{background:#0b4a66;border-color:var(--cyan)}#talk{font-size:18px;padding:8px 12px}#talk.on{background:#064e3b;border-color:#34d399}
.reminder{border-color:var(--gold);color:#fff4dc}
</style></head><body>
<header><div class="orb"></div><h1>JARVIS</h1><span id="state">connecting...</span></header>
<div id="pc" class="notice" style="padding:4px 16px"></div>
<div id="pair" style="display:none;margin:12px 16px;padding:12px;border:1px solid var(--cyan);border-radius:12px">
<div>Pair this phone: on the PC open Menu &rarr; Phone remote and type the 6-digit code shown there.</div>
<input id="code" inputmode="numeric" maxlength="6" placeholder="123456" style="margin-top:8px;width:9em">
<button onclick="pairPhone()">Pair</button><div id="pairmsg" class="notice"></div></div>
<div id="log"></div>
<div id="ask"><div id="question"></div><button onclick="answer(true)">Yes</button><button onclick="answer(false)">No</button></div>
<div class="chips"><button onclick="say('good morning')">Briefing</button><button onclick="say('what\\'s the weather')">Weather</button>
<button onclick="say('pause')">Play / pause</button><button onclick="say('next song')">Next</button>
<button onclick="say('volume down')">Vol -</button><button onclick="say('volume up')">Vol +</button>
<button onclick="say('lock my pc')">Lock PC</button></div>
<div class="chips"><a href="/mobile/app.apk"><button type="button">Get the JARVIS app (wake + control)</button></a>
<button type="button" onclick="wakeSetup()">Set up the wake app</button></div>
<div id="hint" class="notice" style="padding:0 16px"></div>
<form onsubmit="event.preventDefault();say(document.getElementById('text').value)">
<button type="button" id="talk" title="Hold to talk">🎤</button>
<input id="text" autocomplete="off" placeholder="Message Jarvis... (Bangla or English)"><button class="send">Send</button></form>
<script>
let dev='',key='';try{dev=localStorage.getItem('jarvis_dev')||'';key=localStorage.getItem('jarvis_key')||''}catch(e){}
let last=0;const H=()=>({'Content-Type':'application/json','X-Device':dev,'X-Key':key});
async function pairPhone(){const code=document.getElementById('code').value.trim();
const r=await fetch('/api/pair',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({code,name:(navigator.userAgent.match(/\\(([^;)]+)/)||[,'Phone'])[1]})});
const j=await r.json().catch(()=>({}));if(!j.ok){document.getElementById('pairmsg').textContent=j.error||'Pairing failed.';return}
dev=j.device;key=j.key;try{localStorage.setItem('jarvis_dev',dev);localStorage.setItem('jarvis_key',key)}catch(e){}
document.getElementById('pair').style.display='none';poll()}
async function wakeSetup(){try{const r=await fetch('/api/wake-config',{headers:H()});const j=await r.json();
if(!j.ok){hint(j.error||'Not available');return}if(!j.app_available){hint('Install the wake app first (Get the wake app).')}
location.href=j.link}catch(e){hint('The PC is not reachable.')}}
function hint(t){document.getElementById('hint').textContent=t}
function needPair(){document.getElementById('pair').style.display='block';state('not paired')}
function add(m){const d=document.createElement('div');d.className='m '+m.role;
const w=document.createElement('div');w.className='who';w.textContent=(m.role==='user'?'YOU':m.role==='assistant'?'JARVIS':'')+'  '+m.time;
const t=document.createElement('div');t.textContent=m.text;if(m.role!=='notice')d.appendChild(w);d.appendChild(t);
const log=document.getElementById('log');log.appendChild(d);log.scrollTop=log.scrollHeight}
async function poll(){if(!key){needPair();return}try{const r=await fetch('/api/messages?since='+last,{headers:H()});
if(!r.ok){if(r.status==401){key='';try{localStorage.removeItem('jarvis_key')}catch(e){}needPair()}else state('offline');return}
const j=await r.json();j.messages.forEach(m=>{add(m);last=m.id});state(j.status.state||'ready');
const s=j.status||{};let t='PC online';if(s.battery!==undefined)t+=' · battery '+s.battery+'%'+(s.charging?' (charging)':'');
if(s.version)t+=' · Jarvis '+s.version;if(s.wake)t+=' · remote wake: '+s.wake;document.getElementById('pc').textContent=t;
const a=document.getElementById('ask');if(j.pending){pendingId=j.pending.id;document.getElementById('question').textContent=j.pending.text;
a.style.display='block'}else{pendingId='';a.style.display='none'}}
catch(e){state('offline');document.getElementById('pc').textContent='PC not reachable (off, asleep or away from Wi-Fi). Last update: '+new Date().toLocaleTimeString()}}
function state(s){document.getElementById('state').textContent=s.toLowerCase()}
async function say(text){text=(text||'').trim();if(!text)return;document.getElementById('text').value='';
await fetch('/api/send',{method:'POST',headers:H(),body:JSON.stringify({text})});poll()}
let pendingId='';
async function answer(ok){const r=await fetch('/api/confirm',{method:'POST',headers:H(),body:JSON.stringify({approved:ok,id:pendingId})});
const j=await r.json().catch(()=>({}));if(!j.ok)hint.textContent='That question is no longer waiting.';poll()}
let rec=null,chunks=[],alerts=false;
const talk=document.getElementById('talk'),hint=document.getElementById('hint');
if(!window.isSecureContext||!navigator.mediaDevices){talk.disabled=true;
hint.textContent='Talking from the phone needs the https address: turn on REMOTE_HTTPS in Jarvis settings.'}
async function startTalk(e){e.preventDefault();if(rec)return;try{
const s=await navigator.mediaDevices.getUserMedia({audio:true});chunks=[];rec=new MediaRecorder(s);
rec.ondataavailable=ev=>chunks.push(ev.data);rec.onstop=async()=>{s.getTracks().forEach(t=>t.stop());
const blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});rec=null;talk.classList.remove('on');state('hearing...');
const r=await fetch('/api/voice',{method:'POST',headers:{'X-Device':dev,'X-Key':key,'Content-Type':blob.type},body:blob});
const j=await r.json().catch(()=>({}));if(!j.ok)hint.textContent=j.error||"I didn't catch that.";else hint.textContent='';poll()};
rec.start();talk.classList.add('on');state('listening...')}catch(err){hint.textContent='Microphone: '+err.message}}
function stopTalk(e){e.preventDefault();if(rec&&rec.state==='recording')rec.stop()}
talk.addEventListener('pointerdown',startTalk);talk.addEventListener('pointerup',stopTalk);talk.addEventListener('pointerleave',stopTalk);
function alertUser(m){if(!alerts||!document.hidden)return;try{new Notification('Jarvis',{body:m.text.slice(0,200)})}catch(e){}
if(navigator.vibrate)navigator.vibrate(200)}
if('Notification' in window&&window.isSecureContext){const b=document.createElement('button');b.textContent='🔔 Alerts';
b.onclick=async()=>{alerts=(await Notification.requestPermission())==='granted';b.textContent=alerts?'🔔 On':'🔕 Blocked'};
document.querySelector('.chips').appendChild(b)}
const _add=add;add=function(m){_add(m);if(m.role==='reminder'||m.role==='notice'||m.role==='assistant')alertUser(m)};
setInterval(poll,1200);poll();
</script></body></html>"""
