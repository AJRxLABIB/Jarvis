"""Global hotkeys via Win32 RegisterHotKey (no keyboard hook, no key logging).

Combos without a modifier (e.g. plain Escape) are NOT registered globally, because
that would steal the key from every other program; they work inside the window.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

from app.config.files import normalize_hotkey
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("hotkeys")
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "shift": MOD_SHIFT, "win": MOD_WIN}
_NAMED_VK = {
    "space": 0x20, "enter": 0x0D, "escape": 0x1B, "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "printscreen": 0x2C, "pause": 0x13, **{f"f{i}": 0x6F + i for i in range(1, 25)},
}


def parse_hotkey(combo: str) -> tuple[int, int]:
    """'ctrl+shift+j' -> (MOD_CONTROL | MOD_SHIFT, VK 'J')."""
    parts = normalize_hotkey(combo).split("+")
    *mods, key = parts
    modifiers = 0
    for mod in mods:
        modifiers |= _MODS[mod]
    vk = _NAMED_VK.get(key) or ord(key.upper())
    return modifiers, vk


def is_global_combo(combo: str) -> bool:
    return parse_hotkey(combo)[0] != 0


class HotkeyManager(QObject, QAbstractNativeEventFilter):
    triggered = Signal(str)  # action name

    def __init__(self) -> None:
        QObject.__init__(self)
        QAbstractNativeEventFilter.__init__(self)
        self._ids: dict[int, str] = {}
        self._hwnd = 0
        self.failed: dict[str, str] = {}

    def register(self, hwnd: int, hotkeys: dict[str, str]) -> dict[str, bool]:
        """Register global combos for ``hwnd``. Returns action -> registered?"""
        self._hwnd = hwnd
        results: dict[str, bool] = {}
        if sys.platform != "win32":
            return {action: False for action in hotkeys}
        user32 = ctypes.windll.user32
        for index, (action, combo) in enumerate(hotkeys.items(), start=1):
            modifiers, vk = parse_hotkey(combo)
            if modifiers == 0:
                results[action] = False  # window-local only
                continue
            ok = bool(user32.RegisterHotKey(ctypes.c_void_p(hwnd), index, modifiers | MOD_NOREPEAT, vk))
            results[action] = ok
            if ok:
                self._ids[index] = action
            else:
                self.failed[action] = combo
                log_event(_log, "hotkey_unavailable", action=action, combo=combo)
        return results

    def unregister(self) -> None:
        if sys.platform == "win32":
            for hotkey_id in list(self._ids):
                ctypes.windll.user32.UnregisterHotKey(ctypes.c_void_p(self._hwnd), hotkey_id)
        self._ids.clear()

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if sys.platform == "win32" and event_type in (b"windows_generic_MSG", "windows_generic_MSG"):
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam in self._ids:
                self.triggered.emit(self._ids[msg.wParam])
                return True, 0
        return False, 0


HotkeyCallback = Callable[[str], None]
