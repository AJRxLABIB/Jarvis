"""Sign-in tidy-up: the Ollama desktop app opens its own big window when Windows starts it. For the
first moments after Jarvis starts, that window is minimised (Ollama keeps running in the background)
and the Jarvis interface is brought to the front. Later, a window you open yourself is left alone."""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("ui.startup")
TIDY_SECONDS = 90.0  # Ollama can take a while to open its window after sign-in
CHECK_MS = 1500


def is_ollama_window(window) -> bool:
    return (window.process_name.startswith("ollama") and window.switchable and not window.minimized
            and window.title.strip().lower().startswith("ollama"))


class StartupTidy(QObject):
    def __init__(self, bring_forward: Callable[[], None], *, seconds: float = TIDY_SECONDS, parent=None,
                 windows=None, minimize=None) -> None:
        super().__init__(parent)
        from app.tools import winapi

        self._windows = windows or winapi.top_level_windows
        self._minimize = minimize or (lambda hwnd: winapi.show_window(hwnd, winapi.SW_MINIMIZE))
        self._bring_forward = bring_forward
        self._until = time.monotonic() + seconds
        self._done: set[int] = set()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check)

    def start(self) -> None:
        self.check()
        self.timer.start(CHECK_MS)

    def check(self) -> None:
        if time.monotonic() > self._until:
            self.timer.stop()
            return
        try:
            windows = self._windows()
        except Exception:  # not on Windows, or the window list failed: nothing to tidy
            self.timer.stop()
            return
        tidied = False
        for window in windows:
            if window.hwnd not in self._done and is_ollama_window(window):
                self._done.add(window.hwnd)
                try:
                    self._minimize(window.hwnd)
                    tidied = True
                    log_event(_log, "ollama_window_minimized")
                except Exception:
                    _log.warning("could not minimise the Ollama window", exc_info=True)
        if tidied:
            self._bring_forward()
