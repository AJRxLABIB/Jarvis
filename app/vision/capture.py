"""Screen and window capture.

Captures happen only when a tool (or a diagnostic command the user runs) asks
for one, and the host is told while a capture is in progress so it can show the
SCREEN CAPTURE ACTIVE indicator. Images stay in memory unless the user asks for
a screenshot file. Windows whose title or program matches the blocked list
(password managers by default) are never captured.

Win32 and Pillow's screen grabber are imported inside the functions, so this
module imports on any OS; tests replace :data:`backend`.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.tools.base import ToolError

if TYPE_CHECKING:
    from PIL.Image import Image

    from app.tools.base import ToolContext
    from app.tools.winapi import WindowInfo

DWMWA_EXTENDED_FRAME_BOUNDS = 9
PW_RENDERFULLCONTENT = 2
_PER_MONITOR_AWARE_V2 = -4
MAX_SAVED_SIDE = 16384


def require_pillow() -> Any:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ToolError("Pillow is not installed. Run 'pip install -r requirements.txt'.", "missing_dependency") from exc
    return Image


@dataclass(frozen=True)
class Monitor:
    number: int  # 1 = primary
    left: int
    top: int
    width: int
    height: int
    primary: bool = False

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.left + self.width, self.top + self.height


@dataclass
class Capture:
    image: "Image"  # RGB
    source: str  # human description: "Chrome (GitHub - Google Chrome)", "the whole screen", "monitor 2"
    window: "WindowInfo | None" = None
    taken_at: datetime = field(default_factory=lambda: datetime.now().astimezone())

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size


# ------------------------------------------------------------------ backend


@contextmanager
def physical_pixels() -> Iterator[None]:
    """Use real pixel coordinates in this thread, whatever the display scaling."""
    function = None
    old = None
    if sys.platform == "win32":
        function = getattr(ctypes.windll.user32, "SetThreadDpiAwarenessContext", None)
        if function is not None:
            function.restype = ctypes.c_void_p
            function.argtypes = [ctypes.c_void_p]
            old = function(ctypes.c_void_p(_PER_MONITOR_AWARE_V2))
    try:
        yield
    finally:
        if function is not None and old:
            function(ctypes.c_void_p(old))


class WindowsCaptureBackend:
    """The real capture functions (Windows only)."""

    def monitors(self) -> list[Monitor]:
        _require_windows()
        import win32api

        found = []
        with physical_pixels():
            for handle, _, rect in win32api.EnumDisplayMonitors():
                info = win32api.GetMonitorInfo(handle)
                left, top, right, bottom = info.get("Monitor", rect)
                found.append((bool(info.get("Flags", 0) & 1), left, top, right - left, bottom - top))
        found.sort(key=lambda m: (not m[0], m[1], m[2]))  # primary first, then left to right
        return [Monitor(i + 1, left, top, width, height, primary)
                for i, (primary, left, top, width, height) in enumerate(found)]

    def grab(self, bbox: tuple[int, int, int, int] | None) -> "Image":
        _require_windows()
        require_pillow()
        from PIL import ImageGrab

        with physical_pixels():
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
        return image.convert("RGB")

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """Visible bounds (without the invisible resize border)."""
        _require_windows()
        import win32gui
        from ctypes import wintypes

        with physical_pixels():
            rect = wintypes.RECT()
            result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
            if result == 0 and rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right, rect.bottom
            return tuple(win32gui.GetWindowRect(hwnd))  # type: ignore[return-value]

    def print_window(self, hwnd: int) -> "Image | None":
        """The window's own rendering (works even when other windows cover it). None if it can't."""
        _require_windows()
        Image = require_pillow()
        import win32gui
        import win32ui

        with physical_pixels():
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                return None
            window_dc = win32gui.GetWindowDC(hwnd)
            source_dc = win32ui.CreateDCFromHandle(window_dc)
            memory_dc = source_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            try:
                bitmap.CreateCompatibleBitmap(source_dc, width, height)
                memory_dc.SelectObject(bitmap)
                if not ctypes.windll.user32.PrintWindow(hwnd, memory_dc.GetSafeHdc(), PW_RENDERFULLCONTENT):
                    return None
                info = bitmap.GetInfo()
                if info.get("bmBitsPixel") != 32:
                    return None
                bits = bitmap.GetBitmapBits(True)
                image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
            finally:
                win32gui.DeleteObject(bitmap.GetHandle())
                memory_dc.DeleteDC()
                source_dc.DeleteDC()
                win32gui.ReleaseDC(hwnd, window_dc)
            visible = self.window_rect(hwnd)
        crop = (visible[0] - left, visible[1] - top, visible[2] - left, visible[3] - top)
        if crop != (0, 0, width, height) and 0 <= crop[0] < crop[2] <= width and 0 <= crop[1] < crop[3] <= height:
            image = image.crop(crop)
        return image.copy()

    def windows(self) -> "list[WindowInfo]":
        from app.tools import winapi

        return winapi.top_level_windows()

    def foreground(self) -> int | None:
        _require_windows()
        import win32gui

        return win32gui.GetForegroundWindow() or None

    def console_window(self) -> int | None:
        if sys.platform != "win32":
            return None
        return ctypes.windll.kernel32.GetConsoleWindow() or None


def _require_windows() -> None:
    if sys.platform != "win32":
        raise ToolError("Screen capture only works on Windows.", "unsupported_os")


backend: Any = WindowsCaptureBackend()


# ------------------------------------------------------------------ privacy


def blocked_patterns(text: str) -> list[str]:
    return [p for p in (part.strip().lower() for part in text.split(",")) if p]


def blocked_match(window: "WindowInfo", patterns: list[str]) -> str | None:
    haystack = f"{window.title} {window.process_name}".lower()
    return next((p for p in patterns if p in haystack), None)


def check_not_blocked(windows: "list[WindowInfo]", patterns: list[str]) -> None:
    for window in windows:
        pattern = blocked_match(window, patterns)
        if pattern is not None:
            name = window.title or window.process_name
            raise ToolError(
                f"I won't capture the screen while '{name}' is open, because it matches the private-window "
                f"list ('{pattern}' in SCREEN_BLOCKED_WINDOWS). Minimise or close it first.", "blocked_window")


def _on_screen(window: "WindowInfo") -> bool:
    return window.switchable and not window.minimized


# ------------------------------------------------------------------ targets


def _is_own(window: "WindowInfo", console: int | None) -> bool:
    return window.pid == os.getpid() or (console is not None and window.hwnd == console)


def active_window() -> "WindowInfo":
    """The window the user is working in, skipping the assistant's own window and console."""
    windows = [w for w in backend.windows() if _on_screen(w)]
    console = backend.console_window()
    foreground = backend.foreground()
    for window in windows:
        if window.hwnd == foreground and not _is_own(window, console):
            return window
    for window in windows:  # EnumWindows lists windows front to back
        if not _is_own(window, console):
            return window
    raise ToolError("There is no open window to look at.", "no_window")


def named_window(name: str, ctx: "ToolContext") -> "WindowInfo":
    from app.tools.applications import _target_windows

    display, windows, _ = _target_windows(name, ctx)
    if not windows:
        raise ToolError(f"I can't see a window for '{name}'. Is it open?", "not_found")
    shown = [w for w in windows if not w.minimized]
    if not shown:
        raise ToolError(f"{display} is minimised. Restore it first so I can see it.", "minimized")
    return shown[0]


def describe_window(window: "WindowInfo") -> str:
    program = window.process_name.removesuffix(".exe") or "window"
    return f"{program} ({window.title})" if window.title else program


def pick_monitor(number: int) -> Monitor:
    monitors = backend.monitors()
    for monitor in monitors:
        if monitor.number == number:
            return monitor
    raise ToolError(f"There is no monitor {number}; this PC has {len(monitors)}.", "no_monitor")


def capture_window(window: "WindowInfo", patterns: list[str]) -> Capture:
    check_not_blocked([window], patterns)
    image = None
    try:
        image = backend.print_window(window.hwnd)
    except ToolError:
        raise
    except Exception:
        image = None
    if image is None or image.getbbox() is None:  # some apps (GPU-drawn) render black: copy the screen instead
        image = backend.grab(backend.window_rect(window.hwnd))
    if image.width < 2 or image.height < 2:
        raise ToolError("That window has no visible area to capture.", "empty")
    return Capture(image, describe_window(window), window)


def capture_screen(monitor: int | None, patterns: list[str]) -> Capture:
    check_not_blocked([w for w in backend.windows() if _on_screen(w)], patterns)
    if monitor is None:
        return Capture(backend.grab(None), "the whole screen")
    chosen = pick_monitor(monitor)
    return Capture(backend.grab(chosen.bbox), f"monitor {chosen.number}")


def capture(ctx: "ToolContext", *, target: str = "window", window: str | None = None,
            monitor: int | None = None) -> Capture:
    """Capture what the user asked for. Runs in a worker thread."""
    patterns = blocked_patterns(ctx.settings.screen_blocked_windows)
    if window:
        return capture_window(named_window(window, ctx), patterns)
    if target == "screen" or monitor is not None:
        return capture_screen(monitor, patterns)
    return capture_window(active_window(), patterns)


# ----------------------------------------------------------------- indicator


@contextmanager
def capture_indicator(ctx: "ToolContext") -> Iterator[None]:
    """Tell the host a capture is in progress (SCREEN CAPTURE ACTIVE)."""
    callback: Callable[[bool], None] | None = getattr(ctx, "on_screen_capture", None)
    if callback is not None:
        callback(True)
    try:
        yield
    finally:
        if callback is not None:
            callback(False)


def require_enabled(ctx: "ToolContext") -> None:
    if not ctx.settings.screen_capture_enabled:
        raise ToolError(
            "Screen capture is turned off, so I can't look at your screen. Say yes and I'll turn it on (or: "
            "Settings > Privacy > 'Allow screen capture').", "disabled",
            suggestion={"tool": "change_setting", "arguments": {"setting": "screen capture", "value": "on"}})


# ------------------------------------------------------------------ images


def scaled(image: "Image", max_side: int) -> "Image":
    """A copy no larger than ``max_side`` on its longest side."""
    longest = max(image.size)
    if longest <= max_side:
        return image
    Image = require_pillow()
    factor = max_side / longest
    return image.resize((max(1, round(image.width * factor)), max(1, round(image.height * factor))),
                        Image.Resampling.LANCZOS)


def png_bytes(image: "Image", max_side: int | None = None) -> bytes:
    import io

    buffer = io.BytesIO()
    (scaled(image, max_side) if max_side else image).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# -------------------------------------------------------------------- saving

_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def screenshot_folder(ctx: "ToolContext") -> Path:
    """SCREENSHOT_DIR, or your Downloads folder. Never a protected folder."""
    from app.security.validator import Zone

    policy = ctx.get_path_policy()
    folder = ctx.settings.screenshot_dir
    if folder is None:
        folder = policy.known.get("DOWNLOADS") or Path.home() / "Downloads"
    folder = Path(os.path.realpath(folder))
    if policy.zone(folder) == Zone.PROTECTED:
        raise ToolError(f"Screenshots can't be saved in {folder} (a protected folder). Change SCREENSHOT_DIR.",
                        "protected_path")
    return folder


def save_capture(shot: Capture, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    label = _UNSAFE_NAME.sub(" ", shot.source.split(" (")[0]).strip()[:40] or "screen"
    stem = f"Screenshot {shot.taken_at:%Y-%m-%d %H%M%S} {label}"
    path = folder / f"{stem}.png"
    counter = 2
    while path.exists():
        path = folder / f"{stem} ({counter}).png"
        counter += 1
    scaled(shot.image, MAX_SAVED_SIDE).save(path, format="PNG", optimize=True)
    return path
