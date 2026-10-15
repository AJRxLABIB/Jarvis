"""Thin, fixed wrappers around the Windows APIs the tools need.

Nothing here takes a command string: every function performs one specific
operation with validated, typed parameters. Win32 modules are imported inside
the functions so this module imports on any OS (tests replace these functions).
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

from app.tools.base import ToolError

SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9
WM_CLOSE = 0x0010
GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x00000080
DWMWA_CLOAKED = 14
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP, VK_MENU = 0xAD, 0xAE, 0xAF, 0x12
KEYEVENTF_KEYUP = 0x0002


def is_windows() -> bool:
    return sys.platform == "win32"


def require_windows() -> None:
    if not is_windows():
        raise ToolError("This only works on Windows.", "unsupported_os")


def _win32() -> tuple[Any, Any, Any]:
    require_windows()
    try:
        import win32con  # noqa: F401
        import win32gui
        import win32process
    except ImportError as exc:
        raise ToolError("pywin32 is not installed. Run 'pip install -r requirements.txt'.", "missing_dependency") from exc
    return win32gui, win32process, win32con


# --------------------------------------------------------------------- windows


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    process_name: str
    minimized: bool = False
    owned: bool = False  # has an owner window (dialogs, pop-ups)
    tool_window: bool = False  # WS_EX_TOOLWINDOW: not shown in Alt+Tab
    cloaked: bool = False  # hidden by DWM (e.g. suspended UWP apps, other virtual desktops)

    @property
    def switchable(self) -> bool:
        """Would appear in Alt+Tab: titled, unowned, not a tool window, not cloaked."""
        return bool(self.title) and not (self.owned or self.tool_window or self.cloaked)


def _is_cloaked(hwnd: int) -> bool:
    """UWP apps keep invisible 'cloaked' windows around; they must not count."""
    try:
        value = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        return value.value != 0
    except Exception:
        return False


def top_level_windows() -> list[WindowInfo]:
    """Every visible top-level window, with the flags that decide how it is treated."""
    win32gui, win32process, _ = _win32()
    import psutil

    windows: list[WindowInfo] = []
    names: dict[int, str] = {}

    def callback(hwnd: int, _: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid not in names:
            try:
                names[pid] = psutil.Process(pid).name().lower()
            except Exception:
                names[pid] = ""
        windows.append(WindowInfo(
            hwnd=hwnd, pid=pid, title=win32gui.GetWindowText(hwnd).strip(), process_name=names[pid],
            minimized=bool(win32gui.IsIconic(hwnd)), owned=bool(win32gui.GetWindow(hwnd, 4)),  # GW_OWNER
            tool_window=bool(win32gui.GetWindowLong(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW),
            cloaked=_is_cloaked(hwnd),
        ))
        return True

    win32gui.EnumWindows(callback, None)
    return windows


def visible_windows() -> list[WindowInfo]:
    """Top-level windows a user would see in Alt+Tab."""
    return [w for w in top_level_windows() if w.switchable]


def close_window(hwnd: int) -> None:
    win32gui, _, _ = _win32()
    win32gui.PostMessage(hwnd, WM_CLOSE, 0, 0)


def window_exists(hwnd: int) -> bool:
    win32gui, _, _ = _win32()
    return bool(win32gui.IsWindow(hwnd)) and bool(win32gui.IsWindowVisible(hwnd))


def show_window(hwnd: int, command: int) -> None:
    win32gui, _, _ = _win32()
    win32gui.ShowWindow(hwnd, command)


def window_state(hwnd: int) -> str:
    """ "minimized", "maximized", "normal" or "gone" - to check that a window action really happened."""
    win32gui, _, _ = _win32()
    if not win32gui.IsWindow(hwnd):
        return "gone"
    if win32gui.IsIconic(hwnd):
        return "minimized"
    if ctypes.windll.user32.IsZoomed(hwnd):
        return "maximized"
    return "normal"


def foreground_window() -> int:
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def focus_window(hwnd: int) -> bool:
    win32gui, _, _ = _win32()
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, SW_RESTORE)
    user32 = ctypes.windll.user32
    # Windows only lets the foreground process change focus; a synthetic Alt
    # press is the documented-by-practice way to be allowed to do it.
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        return False
    return True


def force_foreground(hwnd: int) -> bool:
    """Bring a window of ours in front of everything, even when another app (Chrome) has the focus.

    Windows refuses SetForegroundWindow to a process that isn't in front; in order: restore it, make it
    topmost for a moment (that alone puts it above other windows), then hand it the focus with the Alt
    trick, and as a last resort by attaching to the foreground window's input thread."""
    if not is_windows():
        return False
    user32 = ctypes.windll.user32
    SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        if user32.SetForegroundWindow(hwnd):
            return True
        foreground = user32.GetForegroundWindow()
        theirs = user32.GetWindowThreadProcessId(foreground, None)
        ours = ctypes.windll.kernel32.GetCurrentThreadId()
        if theirs and theirs != ours and user32.AttachThreadInput(ours, theirs, True):
            try:
                user32.BringWindowToTop(hwnd)
                return bool(user32.SetForegroundWindow(hwnd))
            finally:
                user32.AttachThreadInput(ours, theirs, False)
    except Exception:
        return False
    return False


# ------------------------------------------------------------------ window geometry (layouts)


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class Monitor:
    area: Rect  # the work area: the screen without the taskbar
    primary: bool


def monitors() -> list[Monitor]:
    """Every screen, primary first, then left to right."""
    require_windows()
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    found: list[Monitor] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                       wintypes.LPARAM)

    def callback(hmonitor, _hdc, _rect, _data) -> int:
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            work = info.rcWork
            found.append(Monitor(Rect(work.left, work.top, work.right, work.bottom), bool(info.dwFlags & 1)))
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_type(callback), 0)
    return sorted(found, key=lambda m: (not m.primary, m.area.left, m.area.top))


def window_rect(hwnd: int) -> Rect:
    """The window as you see it (without the invisible resize border Windows 10/11 adds)."""
    require_windows()
    from ctypes import wintypes

    rect = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))  # DWMWA_EXTENDED_FRAME_BOUNDS failed
    return Rect(rect.left, rect.top, rect.right, rect.bottom)


def move_window(hwnd: int, rect: Rect) -> None:
    """Place a window so what you SEE fills ``rect`` exactly (the invisible border is added back)."""
    require_windows()
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    outer, visible = wintypes.RECT(), window_rect(hwnd)
    user32.GetWindowRect(hwnd, ctypes.byref(outer))
    left, top = visible.left - outer.left, visible.top - outer.top
    right, bottom = outer.right - visible.right, outer.bottom - visible.bottom
    if not all(0 <= m <= 20 for m in (left, top, right, bottom)):
        left = top = right = bottom = 0
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    user32.SetWindowPos(hwnd, 0, rect.left - left, rect.top - top, rect.width + left + right,
                        rect.height + top + bottom, SWP_NOZORDER | SWP_NOACTIVATE)


def process_path(pid: int) -> str | None:
    import psutil

    try:
        return psutil.Process(pid).exe()
    except Exception:
        return None


def seconds_since_input() -> float:
    """How long ago you last pressed a key or moved the mouse."""
    if not is_windows():
        return 1e9
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 1e9
    return max(0.0, ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0)


def set_topmost(hwnd: int, on: bool) -> None:
    """Keep a window above all others (ALWAYS_ON_TOP / "stay on top")."""
    if not is_windows():
        return
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
    ctypes.windll.user32.SetWindowPos(hwnd, -1 if on else -2, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def allow_any_foreground() -> None:
    """The process in front lets another of ours take the focus next (the intro hands over to Jarvis)."""
    if is_windows():
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            pass


VK_CONTROL, VK_S = 0x11, 0x53


def press_ctrl_s() -> None:
    """Ctrl+S in the foreground window (the usual 'save' shortcut)."""
    require_windows()
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_S, 0, 0, 0)
    user32.keybd_event(VK_S, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def window_title(hwnd: int) -> str:
    win32gui, _, _ = _win32()
    try:
        return win32gui.GetWindowText(hwnd).strip()
    except Exception:
        return ""


def minimize_all() -> None:
    require_windows()
    import win32com.client

    win32com.client.Dispatch("Shell.Application").MinimizeAll()


# ---------------------------------------------------------------- launching


def start_file(target: str) -> None:
    """Open an executable, shortcut or URI through the Windows shell (no command line)."""
    require_windows()
    try:
        os.startfile(target)  # type: ignore[attr-defined]
    except OSError as exc:
        raise ToolError(f"Windows could not start it: {exc.strerror or exc}", "launch_failed") from exc


def start_program(exe: str, arguments: list[str], *, hidden_console: bool = False,
                  env: dict[str, str] | None = None) -> None:
    """Start an executable with a fixed list of arguments (shell=False, no console window, detached).

    ``hidden_console``: for console programs that start more console programs (ollama serve starts a
    runner per model). Detached, each child would open its own terminal window; with a hidden console
    the children share it and nothing appears."""
    require_windows()
    if hidden_console:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([exe, *arguments], shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags, env=env)
    except OSError as exc:
        raise ToolError(f"Windows could not start it: {exc.strerror or exc}", "launch_failed") from exc


def default_browser_progid() -> str | None:
    """ProgId of the browser Windows opens https links with, e.g. 'ChromeHTML', 'MSEdgeHTM'."""
    if not is_windows():
        return None
    import winreg

    key_path = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "ProgId")
    except OSError:
        return None
    return str(value) if value else None


def app_paths_lookup(exe_name: str) -> str | None:
    """Look up HKCU/HKLM ...\\CurrentVersion\\App Paths\\<exe> (how Win+R finds apps)."""
    if not is_windows():
        return None
    import winreg

    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "")
        except OSError:
            continue
        path = os.path.expandvars(str(value).strip().strip('"'))
        if path and Path(path).is_file():
            return path
    return None


_APPS_FOLDER = ("shell:AppsFolder", "shell:::{4234d49b-0245-4df3-b780-3893943456e1}")


def apps_folder_items() -> list[tuple[str, str]]:
    """(name, app id) for every app in Start, including Microsoft Store apps.

    Store apps (WhatsApp, Spotify from the Store, Calculator, ...) have no .lnk
    shortcut; the shell's Apps folder lists them with their AppUserModelID.
    """
    require_windows()
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    shell = win32com.client.Dispatch("Shell.Application")
    folder = None
    for name in _APPS_FOLDER:
        folder = shell.NameSpace(name)
        if folder is not None:
            break
    if folder is None:
        return []
    items = []
    for item in folder.Items():
        name, app_id = str(item.Name or "").strip(), str(item.Path or "").strip()
        if name and app_id:
            items.append((name, app_id))
    return items


def apps_folder_target(app_id: str) -> str:
    """Shell target that launches an Apps-folder entry."""
    return f"shell:AppsFolder\\{app_id}"


# ------------------------------------------------------------- files / folders

KNOWN_FOLDER_IDS = {
    "DESKTOP": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "DOCUMENTS": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "DOWNLOADS": "{374DE290-123F-4565-9164-39C4925E467B}",
    "PICTURES": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "MUSIC": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "VIDEOS": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
}


def known_folder(key: str) -> Path | None:
    """Real location of a user folder (follows OneDrive/redirection), or None."""
    if not is_windows() or key not in KNOWN_FOLDER_IDS:
        return None
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    guid = GUID()
    if ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(KNOWN_FOLDER_IDS[key]), ctypes.byref(guid)) != 0:
        return None
    buffer = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buffer)) != 0:
        return None
    try:
        return Path(buffer.value) if buffer.value else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(buffer)


def recycle_bin_info() -> tuple[int, int]:
    """(number of items, total bytes) in the Recycle Bin, all drives."""
    require_windows()

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]

    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0, 0
    return int(info.i64NumItems), int(info.i64Size)


def empty_recycle_bin() -> None:
    """Permanently delete everything in the Recycle Bin (no Windows dialog, progress or sound)."""
    require_windows()
    flags = 0x1 | 0x2 | 0x4  # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags)
    if result not in (0, -2147418113):  # S_OK, or E_UNEXPECTED when it was already empty
        raise ToolError(f"Windows could not empty the Recycle Bin (error {result & 0xFFFFFFFF:#x}).", "failed")


def recycle(path: str) -> None:
    """Move a file or folder to the Recycle Bin (never a permanent delete)."""
    require_windows()
    try:
        from win32com.shell import shell, shellcon
    except ImportError as exc:
        raise ToolError("pywin32 is not installed. Run 'pip install -r requirements.txt'.", "missing_dependency") from exc
    flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI
    try:
        result, aborted = shell.SHFileOperation((0, shellcon.FO_DELETE, path, None, flags, None, None))
    except Exception as exc:
        raise ToolError(f"Windows could not move it to the Recycle Bin: {exc}", "delete_failed") from exc
    if result != 0 or aborted:
        raise ToolError(f"Windows could not move it to the Recycle Bin (error {result}).", "delete_failed")


def is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & 0x2 or attributes & 0x4)  # FILE_ATTRIBUTE_HIDDEN / SYSTEM


# --------------------------------------------------------------------- power


def lock_workstation() -> None:
    require_windows()
    if not ctypes.windll.user32.LockWorkStation():
        raise ToolError("Windows refused to lock the computer.", "failed")


def suspend() -> None:
    require_windows()
    # (hibernate=False, force=True, wakeupEventsDisabled=False). If hibernation is
    # enabled system-wide, Windows may hibernate instead of sleeping.
    if not ctypes.windll.powrprof.SetSuspendState(0, 1, 0):
        raise ToolError("Windows refused to go to sleep.", "failed")


def hibernate() -> None:
    """Only when `powercfg /a` lists hibernate (the tool checks); saves everything to disk and powers off."""
    require_windows()
    if not ctypes.windll.powrprof.SetSuspendState(1, 1, 0):
        raise ToolError("Windows refused to hibernate (hibernation may be switched off: powercfg /h on).", "failed")


def _shutdown_exe() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(root) / "System32" / "shutdown.exe")  # absolute: no PATH lookup


def run_shutdown(kind: str, delay: int = 0) -> None:
    """kind: restart | shutdown | sign_out | cancel. Fixed argument lists only."""
    require_windows()
    delay = max(0, min(int(delay), 3600))
    argv = {
        "restart": [_shutdown_exe(), "/r", "/t", str(delay)],
        "shutdown": [_shutdown_exe(), "/s", "/t", str(delay)],
        "sign_out": [_shutdown_exe(), "/l"],
        "cancel": [_shutdown_exe(), "/a"],
    }[kind]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10, shell=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if kind == "cancel" and result.returncode == 1116:
            raise ToolError("No shutdown or restart was scheduled.", "nothing_to_cancel")
        raise ToolError(f"Windows refused: {detail or 'error ' + str(result.returncode)}", "failed")


# -------------------------------------------------------------------- power capabilities (read-only)


def power_states_text() -> str:
    """`powercfg /a`: which sleep states this PC supports (read-only; fixed arguments)."""
    require_windows()
    result = subprocess.run([_system32("powercfg.exe"), "/a"], capture_output=True, text=True, timeout=15,
                            shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return (result.stdout or "") + (result.stderr or "")


_WOL_SCRIPT = ("Get-NetAdapter -Physical | ForEach-Object { $p = Get-NetAdapterPowerManagement -Name $_.Name "
               "-ErrorAction SilentlyContinue; [pscustomobject]@{Name=$_.Name; Media=$_.MediaType; "
               "Desc=$_.InterfaceDescription; Mac=$_.MacAddress; Status=$_.Status; "
               "Magic=if($p){[string]$p.WakeOnMagicPacket}else{''}} } | ConvertTo-Json -Compress")


def adapter_wake_settings() -> list[dict]:
    """Physical network adapters and whether "wake on magic packet" is on (read-only PowerShell, fixed script)."""
    import json as _json

    require_windows()
    powershell = _system32(r"WindowsPowerShell\v1.0\powershell.exe")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", _WOL_SCRIPT],
                            capture_output=True, text=True, timeout=30, shell=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        data = _json.loads(result.stdout or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else [data] if isinstance(data, dict) else []


def fast_startup_enabled() -> bool | None:
    """Windows "Fast startup" (hybrid shutdown) often stops Wake-on-LAN from a full shutdown."""
    if not is_windows():
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Power") as key:
            value, _ = winreg.QueryValueEx(key, "HiberbootEnabled")
        return bool(value)
    except OSError:
        return None


# -------------------------------------------------------------------- volume


def _endpoint_volume() -> Any:
    require_windows()
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError as exc:
        raise ToolError("pycaw is not installed. Run 'pip install -r requirements.txt'.", "missing_dependency") from exc
    comtypes.CoInitialize()  # tools run in worker threads; COM is per-thread
    device = AudioUtilities.GetSpeakers()
    volume = getattr(device, "EndpointVolume", None)
    if volume is None:  # pycaw before 2024 returned the raw IMMDevice
        interface = device.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        volume = interface.QueryInterface(IAudioEndpointVolume)
    return volume


def get_volume() -> tuple[int, bool]:
    volume = _endpoint_volume()
    return round(volume.GetMasterVolumeLevelScalar() * 100), bool(volume.GetMute())


def set_volume(percent: int) -> None:
    _endpoint_volume().SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)


def set_mute(muted: bool) -> None:
    _endpoint_volume().SetMute(1 if muted else 0, None)


# -------------------------------------------------------------------- screen brightness
#
# Laptop screens: WMI (root\\WMI WmiMonitorBrightnessMethods), the same thing the Windows brightness
# slider uses. External monitors: DDC/CI through dxva2 (the monitor's own brightness setting; most
# monitors support it, some have it switched off in their on-screen menu).


def _wmi_brightness() -> Any:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()  # tools run in worker threads; COM is per-thread
    return win32com.client.GetObject("winmgmts:\\\\.\\root\\WMI")


def _wmi_get_brightness() -> int | None:
    try:
        for item in _wmi_brightness().ExecQuery("SELECT CurrentBrightness FROM WmiMonitorBrightness"):
            return int(item.CurrentBrightness)
    except Exception:
        return None
    return None


def _wmi_set_brightness(percent: int) -> bool:
    try:
        done = False
        for item in _wmi_brightness().InstancesOf("WmiMonitorBrightnessMethods"):
            item.WmiSetBrightness(0, int(percent))  # (timeout seconds, brightness percent)
            done = True
        return done
    except Exception:
        return False


class _PhysicalMonitor(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_void_p), ("description", ctypes.c_wchar * 128)]


def _physical_monitors() -> list[_PhysicalMonitor]:
    from ctypes import wintypes

    dxva2, user32 = ctypes.windll.dxva2, ctypes.windll.user32
    _dxva2_types(dxva2)
    monitors: list[_PhysicalMonitor] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                       wintypes.LPARAM)

    def callback(hmonitor, _hdc, _rect, _data) -> int:
        count = wintypes.DWORD(0)
        if dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)) and count.value:
            array = (_PhysicalMonitor * count.value)()
            if dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, count.value, array):
                monitors.extend(array)
        return 1

    user32.EnumDisplayMonitors(None, None, callback_type(callback), 0)
    return monitors


def _ddc(action: Callable[[Any, Any], Any]) -> list[Any]:
    """Run ``action(dxva2, monitor handle)`` on every external monitor; the handles are always released."""
    try:
        dxva2 = ctypes.windll.dxva2
        monitors = _physical_monitors()
    except Exception:
        return []
    results = []
    try:
        for monitor in monitors:
            try:
                results.append(action(dxva2, monitor.handle))
            except Exception:
                results.append(None)
    finally:
        for monitor in monitors:
            try:
                dxva2.DestroyPhysicalMonitor(monitor.handle)
            except Exception:
                pass
    return results


_VCP_BRIGHTNESS = 0x10  # the MCCS "luminance" control every DDC/CI monitor has


def _dxva2_types(dxva2: Any) -> None:
    """64-bit handles must not be cut to 32-bit ints: declare the argument types once."""
    from ctypes import wintypes

    if getattr(dxva2, "_jarvis_typed", False):
        return
    dword_p = ctypes.POINTER(wintypes.DWORD)
    dxva2.GetMonitorBrightness.argtypes = [wintypes.HANDLE, dword_p, dword_p, dword_p]
    dxva2.SetMonitorBrightness.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [wintypes.HANDLE, wintypes.BYTE, ctypes.c_void_p, dword_p,
                                                      dword_p]
    dxva2.SetVCPFeature.argtypes = [wintypes.HANDLE, wintypes.BYTE, wintypes.DWORD]
    dxva2.DestroyPhysicalMonitor.argtypes = [wintypes.HANDLE]
    dxva2.GetCapabilitiesStringLength.argtypes = [wintypes.HANDLE, dword_p]
    dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, dword_p]
    dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, wintypes.DWORD, ctypes.c_void_p]
    dxva2._jarvis_typed = True


def _ddc_range(dxva2: Any, handle: Any) -> tuple[int, int, int] | None:
    """(low, now, high) from the high-level call, else from VCP 0x10 directly (monitors whose capabilities
    string Windows can't parse fail the first but answer the second). DDC is slow and flaky: 3 tries."""
    from ctypes import wintypes

    _dxva2_types(dxva2)
    for attempt in range(3):
        low, now, high = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        if dxva2.GetMonitorBrightness(handle, ctypes.byref(low), ctypes.byref(now), ctypes.byref(high)) \
                and high.value > low.value:
            return low.value, now.value, high.value
        if dxva2.GetVCPFeatureAndVCPFeatureReply(handle, _VCP_BRIGHTNESS, None, ctypes.byref(now),
                                                 ctypes.byref(high)) and high.value > 0:
            return 0, now.value, high.value
        time.sleep(0.05 * (attempt + 1))
    return None


def _ddc_read(dxva2: Any, handle: Any) -> int | None:
    found = _ddc_range(dxva2, handle)
    if found is None:
        return None
    low, now, high = found
    return round((now - low) * 100 / (high - low))


def _ddc_write(percent: int) -> Callable[[Any, Any], bool]:
    def write(dxva2: Any, handle: Any) -> bool:
        found = _ddc_range(dxva2, handle)
        if found is None:
            return False
        low, _now, high = found
        value = low + round((high - low) * percent / 100)
        for attempt in range(3):
            if dxva2.SetMonitorBrightness(handle, value) or dxva2.SetVCPFeature(handle, _VCP_BRIGHTNESS, value):
                return True
            time.sleep(0.05 * (attempt + 1))
        return False
    return write


def brightness_report() -> dict[str, Any]:
    """What this PC offers for brightness, per screen - to explain honestly why it can or can't be set.
    {"laptop_screen": bool, "monitors": [{"name", "ddc": bool, "vcp": bool, "capabilities": bool}]}"""
    require_windows()
    from ctypes import wintypes

    report: dict[str, Any] = {"laptop_screen": _wmi_get_brightness() is not None, "monitors": []}
    try:
        dxva2 = ctypes.windll.dxva2
        monitors = _physical_monitors()
    except Exception:
        return report
    try:
        for monitor in monitors:
            low, now, high = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
            entry = {"name": monitor.description.strip() or "monitor", "ddc": False, "vcp": False,
                     "capabilities": False}
            try:
                entry["ddc"] = bool(dxva2.GetMonitorBrightness(monitor.handle, ctypes.byref(low), ctypes.byref(now),
                                                                ctypes.byref(high)))
                entry["vcp"] = bool(dxva2.GetVCPFeatureAndVCPFeatureReply(
                    monitor.handle, _VCP_BRIGHTNESS, None, ctypes.byref(now), ctypes.byref(high)))
                length = wintypes.DWORD()
                entry["capabilities"] = bool(dxva2.GetCapabilitiesStringLength(monitor.handle, ctypes.byref(length))
                                             and length.value)
            except Exception:
                pass
            report["monitors"].append(entry)
    finally:
        for monitor in monitors:
            try:
                dxva2.DestroyPhysicalMonitor(monitor.handle)
            except Exception:
                pass
    return report


def get_brightness() -> int | None:
    """Screen brightness in percent (the laptop screen, else the first monitor that reports it); None = unknown."""
    require_windows()
    level = _wmi_get_brightness()
    if level is not None:
        return level
    return next((value for value in _ddc(_ddc_read) if value is not None), None)


def set_brightness(percent: int) -> int:
    """Set every screen that allows it to ``percent``. Returns how many screens changed (0 = none could)."""
    require_windows()
    percent = max(0, min(100, int(percent)))
    changed = 1 if _wmi_set_brightness(percent) else 0
    changed += sum(1 for ok in _ddc(_ddc_write(percent)) if ok)
    return changed


def press_key(key: int) -> None:
    """Press and release one virtual key (media keys and the like)."""
    require_windows()
    user32 = ctypes.windll.user32
    user32.keybd_event(key, 0, 0, 0)
    user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)


def press_volume_key(key: int, times: int = 1) -> None:
    """Fallback when the audio endpoint API is unavailable (each press is ~2%)."""
    require_windows()
    user32 = ctypes.windll.user32
    for _ in range(max(1, min(times, 50))):
        user32.keybd_event(key, 0, 0, 0)
        user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.01)


# ---------------------------------------------------------------- mouse and keyboard (screen agent)

_MOUSE = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
_VK_NAMES = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B, "enter": 0x0D,
    "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
    "del": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "insert": 0x2D, "printscreen": 0x2C,
    "plus": 0xBB, "equals": 0xBB, "minus": 0xBD, "period": 0xBE, "comma": 0xBC, "slash": 0xBF, "backtick": 0xC0,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}


def click_at(x: int, y: int, button: str = "left", double: bool = False) -> None:
    """Move the mouse to screen pixel (x, y) and click."""
    require_windows()
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    down, up = _MOUSE[button]
    for _ in range(2 if double else 1):
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.05)


def scroll(amount: int) -> None:
    """Mouse wheel: positive = up, one unit = one notch."""
    require_windows()
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(amount) * 120, 0)  # MOUSEEVENTF_WHEEL


def key_code(name: str) -> int:
    name = name.strip().lower()
    if name in _VK_NAMES:
        return _VK_NAMES[name]
    if len(name) == 1 and (name.isalnum()):
        return ord(name.upper())
    raise ToolError(f"Unknown key: {name}", "bad_key")


def press_combo(combo: str) -> None:
    """ "ctrl+s", "alt+tab", "enter": press the keys together, release in reverse order."""
    require_windows()
    codes = [key_code(part) for part in combo.replace(" ", "").split("+") if part]
    if not codes:
        raise ToolError("Which keys?", "bad_key")
    user32 = ctypes.windll.user32
    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
    for code in reversed(codes):
        user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)


def type_unicode(text: str) -> None:
    """Type text into the focused control (Unicode: Bangla works too)."""
    require_windows()
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    KEYEVENTF_UNICODE = 0x0004
    for ch in text:
        if ch == "\n":
            press_combo("enter")
            continue
        code = ord(ch)
        units = [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
        for unit in units:
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                event = INPUT(type=1)  # INPUT_KEYBOARD
                event.ki = KEYBDINPUT(0, unit, flags, 0, 0)
                ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        time.sleep(0.004)


# ------------------------------------------------------------------ clipboard

CF_UNICODETEXT = 13


def get_clipboard_text() -> str:
    """The text on the clipboard ("" if it holds no text)."""
    require_windows()
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes, kernel32.GlobalLock.restype = [wintypes.HGLOBAL], ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    for _ in range(5):  # another program may have it open for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise ToolError("The clipboard is busy; try again.", "clipboard_busy")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    require_windows()
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes, kernel32.GlobalAlloc.restype = [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes, kernel32.GlobalLock.restype = [wintypes.HGLOBAL], ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    handle = kernel32.GlobalAlloc(0x0002, size)  # GMEM_MOVEABLE
    if not handle:
        raise ToolError("Could not copy to the clipboard.", "clipboard_failed")
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, data, size)
    kernel32.GlobalUnlock(handle)
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise ToolError("The clipboard is busy; try again.", "clipboard_busy")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise ToolError("Could not copy to the clipboard.", "clipboard_failed")
    finally:
        user32.CloseClipboard()


# ------------------------------------------------------------------ Windows Update

def _system32(name: str) -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(root) / "System32" / name)  # absolute: no PATH lookup


def start_windows_update() -> None:
    """Ask Windows Update to check, download and install now (UsoClient, fixed arguments; the update
    service does the work with its own rights). Windows decides when a restart is needed."""
    start_program(_system32("UsoClient.exe"), ["StartInteractiveScan"])
    start_program(_system32("UsoClient.exe"), ["ScanInstallWait"])


def _mpcmdrun() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"


def defender_signature_version() -> str | None:
    """Defender's current security-intelligence version (read-only PowerShell, no admin needed)."""
    require_windows()
    try:
        done = subprocess.run([_system32(r"WindowsPowerShell\v1.0\powershell.exe"), "-NoProfile", "-NonInteractive",
                               "-Command", "(Get-MpComputerStatus).AntivirusSignatureVersion"], capture_output=True,
                              text=True, timeout=30, shell=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = done.stdout.strip().splitlines()[-1].strip() if done.stdout.strip() else ""
    return version if re.fullmatch(r"\d+(?:\.\d+){1,3}", version) else None


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong), ("hIconOrMonitor", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]


def run_elevated(program: Path, parameters: str, timeout_seconds: int = 300) -> int | None:
    """Run a FIXED program with FIXED parameters as administrator. Windows shows its own UAC prompt: nothing is
    elevated unless you press Yes there. Returns the exit code; None = you said no / it couldn't start / timeout."""
    require_windows()
    from ctypes import wintypes

    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile, info.lpParameters, info.nShow = "runas", str(program), parameters, 0
    shell32, kernel32 = ctypes.windll.shell32, ctypes.windll.kernel32
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    if not shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
        return None  # 1223 = the user pressed No on the UAC prompt
    try:
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        if kernel32.WaitForSingleObject(info.hProcess, int(timeout_seconds * 1000)) != 0:
            return None
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(info.hProcess)


def update_defender_signatures(timeout_seconds: int = 240, *, allow_elevation: bool = True) -> tuple[bool, str]:
    """Microsoft Defender's own signature update (MpCmdRun -SignatureUpdate, fixed arguments). Without admin
    rights it usually refuses; then (only if allowed) Windows asks you for administrator permission (UAC).
    Verified by the signature version before and after. Returns (done, detail)."""
    require_windows()
    program = _mpcmdrun()
    if not program.exists():
        return False, "Microsoft Defender's updater isn't on this PC"
    before = defender_signature_version()
    try:
        done = subprocess.run([str(program), "-SignatureUpdate"], capture_output=True, text=True, shell=False,
                              timeout=timeout_seconds, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        code: int | None = done.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        code = None
        detail = exc.__class__.__name__
    elevated = False
    if code != 0 and allow_elevation and not is_admin():
        elevated = True
        code = run_elevated(program, "-SignatureUpdate", timeout_seconds)
        if code is None:
            return False, "it needs administrator permission and Windows' permission prompt wasn't accepted"
    if code != 0:
        return False, detail if code is None else f"the updater stopped with code {code}"
    after = defender_signature_version()
    how = " (with your administrator permission)" if elevated else ""
    if before and after and before != after:
        return True, f"updated from {before} to {after}{how}"
    if after:
        return True, f"already the newest ({after}){how}"
    return True, f"the updater finished{how}"


def pending_updates(timeout_seconds: int = 240) -> list[str]:
    """Titles of the updates Windows has found but not installed (Windows Update Agent, read-only search)."""
    require_windows()
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        session = win32com.client.Dispatch("Microsoft.Update.Session")
        searcher = session.CreateUpdateSearcher()
        searcher.Online = True
        result = searcher.Search("IsInstalled=0 and IsHidden=0")
        return [str(result.Updates.Item(i).Title) for i in range(result.Updates.Count)]
    finally:
        pythoncom.CoUninitialize()


# ------------------------------------------------------------------ Wi-Fi

def wifi_name() -> str:
    """The Wi-Fi network (SSID) this PC is connected to, "" if none (netsh, fixed arguments)."""
    require_windows()
    try:
        output = subprocess.run([_system32("netsh.exe"), "wlan", "show", "interfaces"], capture_output=True,
                                text=True, timeout=5, shell=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    import re as _re

    match = _re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", output, _re.M)
    return match.group(1) if match else ""


# ------------------------------------------------------------------ mouse by voice

def cursor_pos() -> tuple[int, int]:
    require_windows()
    from ctypes import wintypes

    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def move_cursor(x: int, y: int) -> None:
    require_windows()
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def click_here(button: str = "left", double: bool = False) -> None:
    x, y = cursor_pos()
    click_at(x, y, button, double)


def screen_size() -> tuple[int, int]:
    require_windows()
    user32 = ctypes.windll.user32
    return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def foreground_is_ours() -> bool:
    """Is the window in front one of Jarvis's own (then keys and scrolls would go to Jarvis, not your app)?"""
    if not is_windows():
        return False
    front = foreground_window()
    if not front:
        return False
    try:
        return any(w.hwnd == front and w.pid == os.getpid() for w in top_level_windows())
    except Exception:
        return False


def target_window(exclude_pid: int | None = None) -> WindowInfo | None:
    """The window you are working in: the foreground one, or (if that is Jarvis itself) the next one below it."""
    exclude_pid = os.getpid() if exclude_pid is None else exclude_pid
    windows = [w for w in visible_windows() if w.pid != exclude_pid]
    front = foreground_window()
    return next((w for w in windows if w.hwnd == front), None) or (windows[0] if windows else None)
