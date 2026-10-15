"""Start with Windows: a value under HKCU\\...\\Run (current user only, no admin rights)."""

from __future__ import annotations

import sys
from pathlib import Path

from app.utils.paths import is_frozen, project_root

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "JarvisAssistant"


def launch_command() -> str:
    if is_frozen():
        return f'"{sys.executable}"'
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")  # no console window at logon
    exe = windowless if windowless.is_file() else python
    return f'"{exe}" "{project_root() / "run.py"}"'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return bool(value)


def set_enabled(enabled: bool) -> None:
    if sys.platform != "win32":
        raise OSError("Starting with Windows is only available on Windows.")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
