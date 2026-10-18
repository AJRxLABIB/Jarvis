"""Wires the dashboard's living details to what really happens (kept out of main_window.py):

* data pulses from the core to the panel that shows the change (CORE_PULSES): an action -> Active modules and the
  Live event log, a reply -> the conversation, an approval request -> the conversation, a status change -> System overview;
* interface sounds (UI_SOUNDS, app/ui/sound_fx.py);
* the core's speaking animation follows the real loudness of the voice (TTSManager.speech_level);
* HIDDEN_PANELS hides dashboard panels by title (they come back when removed from the list).
"""

from __future__ import annotations

CYAN, GOLD, GREEN, RED = (0.3, 0.9, 1.0), (1.0, 0.78, 0.3), (0.35, 1.0, 0.6), (1.0, 0.35, 0.35)
QUIET_STATES = {"LISTENING", "TRANSCRIBING", "RECORDING"}


def hidden_titles(text: str) -> set[str]:
    return {t.strip().lower() for t in (text or "").split(",") if t.strip()}


def wire_fx(window, controller) -> None:
    orb = window.orb
    settings = lambda: controller.ctx.settings  # noqa: E731
    tts = getattr(controller, "tts", None)
    if tts is not None and hasattr(orb, "speech_level") and hasattr(tts, "speech_level"):
        orb.speech_level = tts.speech_level
    from app.ui.sound_fx import SoundFx

    def busy() -> bool:
        return bool(getattr(tts, "is_speaking", False)) or getattr(window, "_last_state", "") in QUIET_STATES

    window.sfx = SoundFx(tts.play_sound if tts is not None else (lambda _clip: None),
                         enabled=lambda: bool(getattr(settings(), "ui_sounds", False)), busy=busy,
                         volume=lambda: float(getattr(settings(), "ui_sound_volume", 0.5)))
    panels = getattr(window, "panels", {})

    def pulse(title: str, color=CYAN) -> None:
        if hasattr(orb, "pulse_to"):
            orb.pulse_to(panels.get(title), color)

    def on_tool(_text: str) -> None:
        pulse("Active modules")
        pulse("Live event log")
        window.sfx.play("tool")

    def on_reply(reply) -> None:
        ok = bool(getattr(reply, "ok", True))
        pulse("Conversation", GREEN if ok else RED)
        window.sfx.play("done" if ok else "error")

    def on_confirmation(_pending) -> None:
        pulse("Conversation", GOLD)
        window.sfx.play("attention")

    controller.tool_event.connect(on_tool)
    controller.assistant_finished.connect(on_reply)
    controller.confirmation_requested.connect(on_confirmation)
    controller.status_changed.connect(lambda _status: pulse("System overview"))
    controller.notice.connect(lambda _text: pulse("Live event log"))
    window._fx_handlers = (on_tool, on_reply, on_confirmation)        # kept alive with the window
    apply_fx_settings(window)


def apply_fx_settings(window) -> None:
    s = window.controller.ctx.settings
    fx = getattr(window.orb, "fx", None)
    if fx is not None:
        fx.pulses_on = bool(getattr(s, "core_pulses", True))
        fx.trails_on = bool(getattr(s, "core_trails", True))
    hidden = hidden_titles(getattr(s, "hidden_panels", ""))
    for title, panel in getattr(window, "panels", {}).items():
        if title == "Conversation":
            continue                                   # the chat is never hidden (it's how you talk to Jarvis)
        panel.setVisible(title.lower() not in hidden)
