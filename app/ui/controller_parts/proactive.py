"""Event bus, notification center, modes and emergency controls, wired into the window's controller.

Part of AssistantController (app/ui/controller.py); the methods use its attributes and signals through ``self``.
"""

from __future__ import annotations

from app.core import emergency, events, modes, notifications
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("ui.controller")


class ProactiveMixin:
    def init_proactive(self) -> None:
        """At start-up: the bus writes to the database, notifications come from events, mode and switches load."""
        database = self.ctx.database
        self.notifications = None
        if database is None:
            return
        from app.memory.settings_store import SettingsStore

        store = SettingsStore(database)
        events.BUS.clear()
        events.BUS.database = database
        self.notifications = notifications.NotificationCenter(database)
        notifications.connect(events.BUS, self.notifications,
                              on_new=lambda *_: self.notifications_changed.emit(self.notifications.unread_count()))
        events.BUS.subscribe(events.MISSION_COMPLETED, self._remember_mission)  # episodic memory
        modes.STATE = modes.ModeState(store)
        emergency.STATE = emergency.Emergency()
        emergency.STATE.load(store)
        self._announce_update(store)
        self._register_this_pc()

    def _remember_mission(self, event) -> None:
        """A finished mission becomes an episodic memory (marked as recorded by Jarvis, not said by you)."""
        if not modes.STATE.current.remember or self.ctx.database is None:
            return
        from datetime import date

        from app.memory.manager import MemoryManager

        title = str(event.data.get("title", "")).strip()[:120]
        if title:
            MemoryManager(self.ctx.database).record_event(f"Completed the mission '{title}' on {date.today():%Y-%m-%d}")

    def _register_this_pc(self) -> None:
        """Detect this PC's power / wake capabilities in the background (PowerShell takes a moment)."""
        import threading as _threading

        from app.core.devices import DeviceRegistry
        from app.tools import winapi

        self._wake_summary = None
        if not winapi.is_windows():
            return

        def work() -> None:
            try:
                device = DeviceRegistry(self.ctx.database).register_self()
                caps = {c.name: c.status for c in device.capabilities}
                self._wake_summary = (f"from sleep {caps.get('wake_from_sleep', 'unknown')}, from shutdown "
                                      f"{caps.get('wake_from_shutdown', 'unknown')}")
            except Exception:
                _log.warning("could not register this PC", exc_info=True)

        _threading.Thread(target=work, name="device-detect", daemon=True).start()

    def _announce_update(self, store) -> None:
        from app import __version__

        previous = store.get("last_version")
        if previous != __version__:
            store.set("last_version", __version__)
            if previous:
                events.BUS.publish(events.JARVIS_UPDATED, version=__version__, previous=f"from {previous}")

    # -------------------------------------------------------------- speaking up (mode-aware)

    def speak_up(self, message: str, priority: str = "normal", event_name: str | None = None, **data) -> None:
        """A notice from Jarvis itself: into the notification center (via an event), shown, and spoken if the mode
        and focus allow it."""
        if event_name is not None:
            try:
                events.BUS.publish(event_name, message=message, **data)
            except Exception:
                _log.warning("could not publish %s", event_name, exc_info=True)
        if self._focus is not None and priority != "urgent":
            self._focus.hold(message)
            log_event(_log, "alert_held_for_focus")
            return
        self.notice.emit(message)
        if modes.STATE.speaks(priority):
            self.tts.speak(message)

    # -------------------------------------------------------------- modes

    def _mode_control(self, name: str) -> str:
        if name == "status":
            mode = modes.STATE.current
            return f"{mode.name.capitalize()} mode: {mode.description}."
        mode = modes.resolve(name)
        if mode is None:
            return "I don't know that mode. Modes: " + ", ".join(modes.MODES) + "."
        previous = modes.STATE.set(mode)
        events.BUS.publish(events.MODE_CHANGED, mode=mode.name, previous=previous.name)
        log_event(_log, "mode_changed", mode=mode.name)
        return f"{mode.name.capitalize()} mode: {mode.description}."

    # -------------------------------------------------------------- emergency

    def emergency_stop(self) -> str:
        """Stop everything now (hotkey Ctrl+Shift+F12, Menu, or by voice)."""
        stopped = []
        try:
            from app.tools.youtube import PLAYER

            if PLAYER.queue:
                PLAYER.clear()
                stopped.append("the song queue")
        except Exception:
            pass
        try:
            from app.security.command_executor import EXECUTOR

            ended = EXECUTOR.stop_all()
            if ended:
                stopped.append(f"{ended} background command(s)")
        except Exception:
            pass
        self.cancel()  # speech, listening, the running request (a mission pauses), power countdowns
        emergency.STATE.set("automation_disabled", True, by_voice=True)
        log_event(_log, "emergency_stop")
        message = "Emergency stop: everything cancelled and automations switched off" + (
            f" ({', '.join(stopped)} cleared)" if stopped else "") + ". Turn them back on in Menu -> Emergency controls."
        self.notice.emit(message)
        return message

    def _emergency_control(self, action: str) -> str:
        """emergency_control tool (assistant thread): voice can switch OFF, never back ON."""
        if action == "stop":
            self._emergency_request.emit()
            return "Stopping everything. Automations are switched off; turn them back on in the window."
        if action == "status":
            active = emergency.STATE.active()
            return "Emergency switches on: " + ", ".join(active) + "." if active else "No emergency switches are on."
        switch = {"disable_automations": "automation_disabled", "disable_remote": "remote_disabled",
                  "disable_network": "network_disabled", "lock": "locked",
                  "disable_evolution": "evolution_disabled"}[action]
        emergency.STATE.set(switch, True, by_voice=True)
        if switch == "remote_disabled":
            self._stop_remote_request.emit()
        return f"Done: {emergency.LABELS[switch]}. Only the window can turn it back on (Menu -> Emergency controls)."

    def set_emergency_switch(self, switch: str, on: bool) -> None:
        """From the window (a human at the PC): both directions allowed."""
        emergency.STATE.set(switch, on, by_voice=False)
        if switch == "remote_disabled":
            if on:
                self.stop_remote()
            else:
                self.start_remote()
