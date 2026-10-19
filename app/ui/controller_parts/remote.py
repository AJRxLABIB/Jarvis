"""The phone remote (pairing v2) and privacy changes.

Part of AssistantController (app/ui/controller.py), split out in 3.4.1; the methods use its attributes
and signals through ``self``."""

from __future__ import annotations

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("ui.controller")


class RemoteMixin:
    def wake_app_config(self) -> dict:
        """What the Jarvis Wake phone app needs to wake this PC (read from Windows, never guessed)."""
        from app.core.devices import DeviceRegistry, subnet_broadcast
        from app.remote.server import lan_address, tailscale_address

        settings = self.ctx.settings
        if self.ctx.database is None:
            return {"error": "no database"}
        registry = DeviceRegistry(self.ctx.database)
        try:
            device = registry.find("this pc")
        except Exception:
            try:
                device = registry.register_self()
            except Exception as exc:
                return {"error": f"couldn't read this PC's network adapters ({exc.__class__.__name__})"}
        address = lan_address()
        caps = {c.name: c for c in device.capabilities}
        wake = caps.get("wake_from_sleep")
        return {
            "name": device.name, "mac": device.mac, "ip": address,
            "broadcast": subnet_broadcast(address) or "", "port": str(settings.remote_port),
            "public": getattr(settings, "wake_public_address", "") or "",
            "public_port": str(getattr(settings, "wake_public_port", 9)),
            "away": (tailscale_address() or "") if getattr(settings, "remote_allow_tailscale", False) else "",
            "wake_status": wake.status if wake else "unknown", "notes": wake.detail if wake else "",
        }

    def remote_status(self) -> dict:
        """What the phone shows about this PC: it's online (the page answered), battery, Jarvis, remote wake."""
        import time as _time

        import psutil

        from app import __version__

        status: dict = {"state": self._state, "online": True, "version": __version__,
                        "uptime_hours": round((_time.time() - psutil.boot_time()) / 3600, 1)}
        try:
            status["cpu"] = round(psutil.cpu_percent(interval=None))
            status["ram"] = round(psutil.virtual_memory().percent)
        except Exception:
            pass
        try:
            from app.remote import pc_state, win_auth

            snap = pc_state.snapshot(jarvis_ready=True, session_state=pc_state.session(), auth=win_auth.cached())
            status.update(snap)  # real Windows lock state + machine state, never faked
        except Exception:
            pass
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                status["battery"] = round(battery.percent)
                status["charging"] = bool(battery.power_plugged)
        except Exception:
            pass
        wake = getattr(self, "_wake_summary", None)
        if wake:
            status["wake"] = wake
        return status

    def start_remote(self) -> str | None:
        """REMOTE_ENABLED: the phone remote web page. Returns its address (with the pairing token)."""
        from app.core import emergency

        if not self.ctx.settings.remote_enabled or emergency.STATE.flags.get("remote_disabled"):
            return None
        if self.remote is not None:
            return self.remote.url
        from app.remote.pairing import PairingStore
        from app.remote.server import RemoteHub, RemoteServer

        def answer(approved: bool, request_id: str = "") -> bool:
            pending = self._pending_confirmation
            if pending is None or pending.future.done():
                return False
            if request_id != pending.request.id:  # the phone answered a question that isn't the one waiting now
                log_event(_log, "remote_answer_stale")
                return False
            if approved and int(getattr(pending.request, "level", 0)) >= 3:   # CRITICAL: only on the PC's screen
                log_event(_log, "remote_critical_refused")
                return False
            pending.future.set_result(approved)
            self.confirmation_answered.emit(pending, approved)
            return True

        def pending_text() -> dict | None:
            pending = self._pending_confirmation
            if pending is None or pending.future.done():
                return None
            return {"id": pending.request.id, "text": pending.request.summary, "level": int(pending.request.level)}

        def heard(audio: bytes) -> str:
            """Speech recorded on the phone -> text (the PC's speech recognition; audio is not kept)."""
            import io

            from faster_whisper import decode_audio

            samples = decode_audio(io.BytesIO(audio), sampling_rate=16000)
            s = self.ctx.settings
            transcript = self.stt.transcribe(samples, None if s.voice_language == "auto" else s.voice_language)
            return (transcript.text or "").strip()

        hub = RemoteHub(self._remote_send.emit, answer, pending_text, self.remote_status, voice=heard,
                        wake_config=self.wake_app_config)
        self.user_message.connect(lambda text: hub.add("user", text))
        self.assistant_finished.connect(lambda reply: hub.add("assistant", getattr(reply, "text", "")))
        self.notice.connect(lambda text: hub.add("notice", text))
        self.reminder.connect(lambda title, message: hub.add("reminder", f"{title}: {message}"))
        context, fingerprint = None, ""
        if self.ctx.settings.remote_https:
            try:
                from app.remote.server import lan_address
                from app.remote.tls import ensure_certificate, fingerprint as cert_fingerprint, server_context

                cert, key = ensure_certificate(self.ctx.paths.data_dir, lan_address())
                context, fingerprint = server_context(cert, key), cert_fingerprint(cert)
            except Exception as exc:
                self.notice.emit(f"HTTPS for the phone remote is unavailable ({exc}); using http (typing only).")
        self.pairing = PairingStore(self.ctx.paths.data_dir)
        self._connect_mobile(hub)
        server = RemoteServer(hub, self.pairing, self.ctx.settings.remote_port, ssl_context=context,
                              allow_tailscale=bool(getattr(self.ctx.settings, "remote_allow_tailscale", False)))
        self.remote_fingerprint = fingerprint
        server.tls_fingerprint = fingerprint
        try:
            self.runner.run(server.start(), timeout=10)
        except Exception as exc:
            self.notice.emit(f"The phone remote could not start: {exc}")
            return None
        self.remote, self.remote_hub = server, hub
        self.notice.emit(f"Phone remote is on: open {server.url} on your phone (same Wi-Fi). Menu -> Phone remote "
                         "shows the pairing code.")
        return server.url

    def mobile_registry(self):
        """JARVIS Mobile phones (public keys, trust, permissions); None without a database."""
        if getattr(self, "_mobile_registry", None) is None and self.ctx.database is not None:
            from app.remote.mobile import MobileRegistry

            registry = MobileRegistry(self.ctx.database)
            registry.on_pending = lambda device: self.mobile_pending.emit(device)
            registry.on_security_event = lambda kind, detail: self.notice.emit(
                f"SECURITY ALERT (JARVIS Mobile): {kind.replace('_', ' ')} - {detail}")
            self._mobile_registry = registry
        return getattr(self, "_mobile_registry", None)

    def mobile_sessions(self):
        """Remote-control sessions (open/terminate/audit); None without a database."""
        if getattr(self, "_mobile_sessions", None) is None and self.ctx.database is not None:
            from app.remote.mobile import SessionStore

            self._mobile_sessions = SessionStore(self.ctx.database)
        return getattr(self, "_mobile_sessions", None)

    def pc_capabilities(self) -> dict:
        """What this PC can do for the phone, and the supported Windows sign-in options - read, not assumed."""
        import socket

        from app.core.devices import DeviceRegistry
        from app.remote import apk, win_auth

        caps = {"wake_on_lan": False, "remote_desktop": False, "screen_stream": bool(self.ctx.settings.screen_capture_enabled),
                "remote_mouse": bool(self.ctx.settings.screen_capture_enabled),
                "remote_keyboard": bool(self.ctx.settings.screen_capture_enabled),
                "file_access": True, "browser_control": True, "power_control": True}
        try:
            auth = win_auth.cached()
            caps["remote_desktop"] = auth.remote_desktop == "available"
            auth_dict = auth.to_dict()
        except Exception:
            auth_dict = {}
        try:
            if self.ctx.database is not None:
                device = DeviceRegistry(self.ctx.database).find("this pc")
                caps["wake_on_lan"] = bool(device.mac)
        except Exception:
            pass
        return {"capabilities": caps, "auth": auth_dict, "app": apk.info(),
                "pc_name": socket.gethostname()[:40]}

    def _connect_mobile(self, hub) -> None:
        """Signed phone commands: chat/approvals through the hub, actions through the executor (safety kernel)."""
        from app.ai.base import ToolCall

        hub.mobile = self.mobile_registry()
        hub.sessions = self.mobile_sessions()
        hub.ops_ctx = self.executor.context
        hub.pc_capabilities = self.pc_capabilities
        hub.pc_wake_config = self.wake_app_config
        hub.emergency = self._mobile_emergency
        hub.phone_screen = self.phone_screens()
        hub.cancel = self._mobile_cancel
        from app.remote.phone_inbox import PhoneInbox

        hub.phone_inbox = PhoneInbox(self.ctx.paths.data_dir)
        hub.clipboard = self._clipboard_from_phone
        hub.on_file = lambda phone, path: self.notice.emit(f"{phone} sent a file: {path.name} (saved in {path.parent}).")

        async def run_tool(tool: str, arguments: dict) -> dict:
            # the phone's button press is your own command, exactly like saying it: same checks, same audit
            execution = await self.executor.execute(ToolCall(tool, arguments), user_command=True)
            result = execution.result
            return {"ok": result.ok, "message": result.message, "error": result.error, "data": result.data}

        hub.run_tool = run_tool

    def _mobile_cancel(self) -> str:
        """The phone's Cancel: stops what Jarvis is doing right now (speaking, listening, the running request, a
        pending question) - NOT the automations (that is the emergency stop). Nothing already done is undone."""
        busy = bool(getattr(self, "_busy", False) or getattr(self, "_listening", False))
        self._cancel_request.emit()
        return ("Cancelled the current request (anything it already did is not undone)." if busy
                else "Nothing was running.")

    def phone_screens(self):
        """Phones sharing their screen with this PC (frames in memory only): app/remote/phone_screen.py."""
        screens = getattr(self, "_phone_screens", None)
        if screens is None:
            from app.remote.phone_screen import PhoneScreenHub

            screens = self._phone_screens = PhoneScreenHub()
        return screens

    def _clipboard_from_phone(self, text: str) -> None:
        """'Send clipboard to PC' on the phone: the text goes onto this PC's clipboard (Windows clipboard API)."""
        from app.tools import winapi

        try:
            winapi.set_clipboard_text(text)
        except Exception:
            _log.warning("could not set the clipboard from the phone", exc_info=True)
        self.notice.emit("Your phone's clipboard is on the PC now (Ctrl+V).")

    def ask_phone_to_share(self) -> str:
        """The PC viewer's "Ask the phone to share": queue share_screen for the most recently seen trusted phone."""
        from app.remote.mobile import MobileRegistry
        from app.remote.phone_link import PhoneLink

        database = getattr(self.ctx, "database", None)
        if database is None:
            return "The phone link needs the local database."
        link = PhoneLink(database)
        phones = [d for d in MobileRegistry(database).all() if d.trust == "trusted" and not d.revoked_at]
        if not phones:
            return "No trusted phone is paired (Menu > Mobile devices)."
        phone = max(phones, key=lambda d: link.state(d.id).get("seen", ""))
        state = link.state(phone.id)
        if state and not state.get("control"):
            return f"{phone.name} doesn't allow control from the PC: JARVIS Mobile > Control > 'Let my PC control this phone'."
        link.queue(phone.id, "share_screen", {})
        return (f"Asked {phone.name}. Tap 'Start now' on the phone (Android asks every time). It reaches the phone the "
                "next time it checks in: at once if JARVIS Mobile is open or 'Stay connected' is on.")

    def _mobile_emergency(self) -> str:
        sessions = self.mobile_sessions()
        if sessions is not None:
            sessions.end_all("emergency stop")
        return self._emergency_control("stop")

    def mobile_pairing_link(self) -> tuple[str, int] | None:
        """(https link for the phone, seconds left) for a new 5-minute pairing session; None when off."""
        from urllib.parse import urlencode

        registry, remote = self.mobile_registry(), getattr(self, "remote", None)
        if registry is None or remote is None:
            return None
        session = registry.start_pairing()
        query = urlencode({"pid": session.id, "s": session.secret})
        return f"{remote.url.rstrip('/')}/mobile/pair?{query}", registry.pairing_left()

    def pairing_code(self) -> str | None:
        """A one-time code (5 minutes) for pairing a phone; None when the remote is off."""
        pairing = getattr(self, "pairing", None)
        return pairing.new_code() if pairing is not None else None

    def privacy_changed(self, key: str) -> None:
        """Privacy dashboard deleted something (or renewed the remote key): drop what is cached in memory."""
        if key == "corrections":
            self.conversation._corrections_store = None
        elif key == "history":
            self.conversation.context.clear()
            self.conversation.taint.clear()
        elif key == "voiceprint" and self.hands_free is not None and self.ctx.settings.speaker_verification:
            self._restart_hands_free.emit()
        elif key == "remote_key" and self.remote is not None:
            self.stop_remote()
            self.start_remote()

    def apply_remote_settings(self, settings) -> None:
        """Phone-remote settings changed: stop it and start it again with the new values (no Jarvis restart)."""
        self.ctx.settings = settings
        self.stop_remote()
        if not settings.remote_enabled:
            self.notice.emit("Phone remote is off.")
            return
        try:
            self.start_remote()
            self.notice.emit("Phone remote is on (Menu > Phone remote shows how to connect).")
        except Exception as exc:
            self.notice.emit(f"The phone remote could not start: {exc}")

    def stop_remote(self) -> None:
        if self.remote is not None:
            try:
                self.runner.run(self.remote.stop(), timeout=5)
            except Exception:
                _log.warning("could not stop the phone remote", exc_info=True)
            self.remote = None
