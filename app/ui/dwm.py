"""Live window pictures from the Windows compositor (DWM thumbnails) - what Alt+Tab and the taskbar previews use.

DWM draws the other window's real, live content straight into a rectangle of OUR window. Jarvis never reads those
pixels: nothing is captured, stored or sent anywhere, it is only shown on your screen while the overview is open.
Windows only; elsewhere every call is a harmless no-op (the overview then shows title cards).
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

DWM_TNP_RECTDESTINATION = 0x1
DWM_TNP_OPACITY = 0x4
DWM_TNP_VISIBLE = 0x8
DWM_TNP_SOURCECLIENTAREAONLY = 0x10


class _Props(ctypes.Structure):
    _fields_ = [("dwFlags", wintypes.DWORD), ("rcDestination", wintypes.RECT), ("rcSource", wintypes.RECT),
                ("opacity", ctypes.c_ubyte), ("fVisible", wintypes.BOOL), ("fSourceClientAreaOnly", wintypes.BOOL)]


def available() -> bool:
    return sys.platform == "win32"


class Thumbnail:
    """One live picture of window ``source`` inside window ``dest`` (both HWNDs)."""

    def __init__(self, dest: int, source: int) -> None:
        self.handle = None
        self.source_size = (0, 0)
        if not available():
            return
        handle = ctypes.c_void_p()
        dwm = ctypes.windll.dwmapi
        if dwm.DwmRegisterThumbnail(wintypes.HWND(dest), wintypes.HWND(source), ctypes.byref(handle)) != 0:
            return
        self.handle = handle
        size = wintypes.SIZE()
        if dwm.DwmQueryThumbnailSourceSize(handle, ctypes.byref(size)) == 0:
            self.source_size = (size.cx, size.cy)

    @property
    def ok(self) -> bool:
        return self.handle is not None

    def place(self, x: int, y: int, w: int, h: int, opacity: float = 1.0, visible: bool = True) -> None:
        """Destination rectangle in physical pixels of the destination window's client area."""
        if self.handle is None:
            return
        props = _Props()
        props.dwFlags = DWM_TNP_RECTDESTINATION | DWM_TNP_OPACITY | DWM_TNP_VISIBLE | DWM_TNP_SOURCECLIENTAREAONLY
        props.rcDestination = wintypes.RECT(int(x), int(y), int(x + w), int(y + h))
        props.opacity = max(0, min(255, int(opacity * 255)))
        props.fVisible = bool(visible and w > 1 and h > 1)
        props.fSourceClientAreaOnly = False
        ctypes.windll.dwmapi.DwmUpdateThumbnailProperties(self.handle, ctypes.byref(props))

    def release(self) -> None:
        if self.handle is not None and available():
            ctypes.windll.dwmapi.DwmUnregisterThumbnail(self.handle)
        self.handle = None
