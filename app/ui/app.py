"""Starts the desktop app: single instance, theme, window, tray, hotkeys."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from app import __app_name__, __version__
from app.ui.controller import AssistantController
from app.ui.hotkeys import HotkeyManager
from app.ui.main_window import MainWindow
from app.ui.styles.theme import STYLESHEET
from app.ui.tray import TrayController, make_icon
from app.utils.logging_setup import get_logger, log_event

if TYPE_CHECKING:
    from app.main import AppContext

_log = get_logger("ui")


def instance_lock_path() -> Path:
    """One lock for the whole user account, so the installed JARVIS.exe and `python run.py` (which keep
    their data in different folders) can never run at the same time and both answer."""
    return Path(tempfile.gettempdir()) / "JarvisAssistant.instance.lock"


def run_gui(ctx: "AppContext", argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(argv or sys.argv[:1])
    app.setApplicationName(__app_name__)
    app.setQuitOnLastWindowClosed(False)  # keeps running in the tray
    app.setStyleSheet(STYLESHEET)
    app.setWindowIcon(make_icon())

    lock = QLockFile(str(instance_lock_path()))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.information(None, __app_name__, "The assistant is already running (see the system tray).")
        return 0

    import time as _time

    from app.utils import crash

    started = _time.perf_counter()
    from app.ui import motion

    motion.set_enabled(getattr(ctx.settings, "animations", True))
    minimized = ctx.settings.start_minimized or "--minimized" in (argv or sys.argv)
    launcher = None
    if not minimized and getattr(ctx.settings, "intro_animation", True) and motion.ENABLED:
        # First thing: the intro, in its own process, so it plays smoothly while everything below loads.
        from app.ui.intro_process import IntroLauncher

        launcher = IntroLauncher(".".join(ctx.settings.assistant_name.upper()) + ".",
                                 f"Personal AI assistant  ·  v{__version__}  ·  all on this PC", lambda: None)
        if not launcher.start():
            launcher = None
    crash_report = crash.start(ctx)
    controller = AssistantController(ctx)
    window = MainWindow(controller, ctx.settings.monitor_interval)
    if crash_report is not None:
        window.chat.add_message("notice", f"Jarvis didn't close normally last time. A crash report (private data "
                                          f"removed) was saved to {crash_report}.")

    def open_logs() -> None:
        tray.show_window()
        window.open_logs()

    def quit_app() -> None:
        window.allow_close = True
        app.quit()

    tray = TrayController(window, controller, open_logs, quit_app)
    tray.show()

    hotkeys = HotkeyManager()
    app.installNativeEventFilter(hotkeys)
    actions = {
        "activate_assistant": tray.show_window,
        "toggle_listening": controller.toggle_listening,
        "cancel": controller.cancel,
        "emergency_stop": controller.emergency_stop,
    }
    hotkeys.triggered.connect(lambda action: actions.get(action, lambda: None)())

    if ctx.settings.start_with_windows:  # keep the registry entry pointing at this installation
        from app.utils import autostart

        try:
            autostart.set_enabled(True)
        except OSError:
            _log.warning("could not register start with Windows", exc_info=True)
    full_screen = getattr(ctx.settings, "start_fullscreen", True)
    if minimized:
        log_event(_log, "started_minimized")
    elif launcher is not None:
        window._started = False
        window._intro_launcher = launcher
        lines = intro_status_lines(ctx, controller)
        built = _time.monotonic()

        def feed() -> None:  # the real states to the intro; the finale brings the window in
            states = []
            for label, state in lines:
                try:
                    text, kind = state()
                except Exception:
                    text, kind = "UNKNOWN", "warn"
                states.append((label, text, kind))
            loaded = controller.tts.available and (controller.hands_free is not None or not ctx.settings.hands_free)
            launcher.send(states, ready=loaded or _time.monotonic() - built > 8.0)
            if launcher.finale_reached:
                feeder.stop()
                window.show_start(full_screen)
                launcher.close()
                log_event(_log, "intro_finished")

        feeder = QTimer(window)
        feeder.timeout.connect(feed)
        feeder.start(150)
        log_event(_log, "intro_started", separate_process=True)
    elif getattr(ctx.settings, "intro_animation", True) and motion.ENABLED:
        from app.ui.intro import IntroScreen

        intro = IntroScreen(".".join(ctx.settings.assistant_name.upper()) + ".",
                            f"Personal AI assistant  ·  v{__version__}  ·  all on this PC",
                            intro_status_lines(ctx, controller))
        intro.finished.connect(lambda: window.show_start(full_screen))
        window._intro = intro  # keep it alive until it closes itself
        window._started = False
        intro.start()
        log_event(_log, "intro_started")
    else:
        window.show_start(full_screen, fade=False)
    if ctx.settings.hide_ollama_window and sys.platform == "win32":
        from app.ui.startup import StartupTidy

        def bring_forward() -> None:  # after Ollama's window moved away, Jarvis is the one you see
            if not minimized:
                tray.show_window()

        tidy = StartupTidy(bring_forward, parent=window)
        QTimer.singleShot(0, tidy.start)
    registered = hotkeys.register(int(window.winId()), dict(ctx.config.shortcuts.hotkeys))
    if hotkeys.failed:
        taken = ", ".join(f"{combo} ({action})" for action, combo in hotkeys.failed.items())
        window.chat.add_message("notice", f"These hotkeys are used by another program: {taken}. "
                                          "Change them in config/shortcuts.json.")
    log_event(_log, "gui_started", hotkeys=registered)
    QTimer.singleShot(0, controller.refresh_status)
    QTimer.singleShot(0, controller.start_scheduler)
    # Heavy parts load after the window is on screen, so Jarvis is usable at once:
    QTimer.singleShot(300, controller.tts.start)  # the voice engine loads in its own thread
    QTimer.singleShot(700, controller.start_hands_free)  # only if HANDS_FREE=true (loads speech recognition)
    QTimer.singleShot(0, lambda: log_event(_log, "window_ready", ms=int((_time.perf_counter() - started) * 1000)))
    if not ctx.settings.setup_done:
        QTimer.singleShot(1200, window.open_setup_wizard)  # first run
    QTimer.singleShot(8000, controller.check_for_updates)  # only if UPDATE_MANIFEST_URL is set
    window.chat.add_message("notice", "Hello. Type a message or press Talk. Hotkeys: "
                                      + ", ".join(f"{a.replace('_', ' ')} = {c}" for a, c in ctx.config.shortcuts.hotkeys.items()))

    try:
        return app.exec()
    finally:
        hotkeys.unregister()
        controller.shutdown()
        tray.tray.hide()
        crash.stop(ctx)
        lock.unlock()


def intro_status_lines(ctx, controller):
    """What the intro's boot log shows: each part's REAL state when the line is drawn (it updates live)."""
    from app.voice.hands_free import resolve_engine
    from app.voice.wake_word import is_bare_name

    def speech_output():
        return ("READY", "ok") if controller.tts.available else ("STARTING", "wait")

    def recognition():
        stt = controller.stt
        if not ctx.settings.microphone_enabled:
            return ("MICROPHONE OFF", "warn")
        return ("READY", "ok") if getattr(stt, "loaded", False) else ("LOADING", "wait")

    def ai_model():
        status = getattr(controller, "_last_status", None) or controller.last_status
        if status is None:
            return ("CONNECTING", "wait")
        if status.reachable and status.selected is not None:
            return (status.selected.name, "ok")
        return ("NOT REACHABLE", "bad")

    def hands_free():
        if controller.hands_free is not None:
            return ("LISTENING", "ok")
        return ("STARTING", "wait") if ctx.settings.hands_free else ("OFF", "info")

    def wake_word():
        engine = resolve_engine(ctx.settings)
        if engine == "openwakeword":
            return ("NEURAL + CHECK" if is_bare_name(ctx.settings.wake_word) else "NEURAL", "info")
        return ("SPEECH RECOGNITION", "info")

    try:
        with ctx.database.connect() as conn:
            facts = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    except Exception:
        facts = None

    def memory():
        return (f"{facts} FACTS" if facts is not None else "UNAVAILABLE", "info" if facts is not None else "warn")

    return [("Speech output", speech_output), ("Speech recognition", recognition), ("AI model", ai_model),
            ("Hands-free", hands_free), ("Wake word", wake_word), ("Memory", memory)]
