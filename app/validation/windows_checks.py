"""Windows validation: UI Automation, DPI awareness and scaling, several monitors, coordinates, focus.

The decisions (what counts as a pass, a warning, a failure) are in the check functions and are tested with a fake ``Env``. ``RealEnv`` is
the only part that touches Windows; it has NOT been run by the automated tests and is exactly what ``python run.py --validate windows``
exercises on your PC. The checks that open an application need ``--launch`` (Notepad is opened, used and closed; nothing is typed or saved).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.validation.report import FAIL, PASS, PENDING, SKIP, WARN, Report, Result, timed

TOLERANCE_PX = 3
UIA_MIN_ELEMENTS = 8
UIA_SLOW_MS = 3000


@dataclass
class MonitorInfo:
    index: int
    left: int
    top: int
    width: int                 # physical pixels
    height: int
    primary: bool
    scale: float               # 1.0 = 100 %
    work: tuple[int, int, int, int] = (0, 0, 0, 0)    # work area as Jarvis's window code sees it (left, top, right, bottom)


@dataclass
class Rects:
    visible: tuple[int, int, int, int]     # winapi.window_rect (DWM frame bounds)
    capture: tuple[int, int, int, int]     # the screen-capture backend
    uia: tuple[int, int, int, int] | None  # UI Automation BoundingRectangle of the window element


class Env:
    """What the checks need from Windows. ``RealEnv`` implements it with the real APIs; tests use a fake."""

    def is_windows(self) -> bool: ...
    def dpi_awareness(self) -> str: ...                          # unaware | system | per-monitor | unknown
    def monitors(self) -> list[MonitorInfo]: ...
    def capture_size(self, monitor: MonitorInfo) -> tuple[int, int]: ...
    def open_notepad(self) -> int: ...                           # hwnd (0 = could not find the window)
    def rects(self, hwnd: int) -> Rects: ...
    def uia_tree(self, hwnd: int) -> tuple[int, int, list[str]]: ...   # (elements, ms, a few names); raises if UIA is unavailable
    def focus(self, hwnd: int) -> bool: ...
    def foreground(self) -> int: ...
    def move_to(self, hwnd: int, monitor: MonitorInfo) -> None: ...
    def close(self, hwnd: int) -> None: ...


def _close(a: tuple[int, int, int, int], b: tuple[int, int, int, int], tol: int = TOLERANCE_PX) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ------------------------------------------------------------------ checks that open nothing


def check_platform(env: Env) -> tuple[str, str]:
    return (PASS, "running on Windows") if env.is_windows() else (PENDING, "not Windows - every Windows check is pending until run on your PC")


def check_dpi(env: Env) -> tuple[str, str]:
    mode = env.dpi_awareness()
    if mode == "per-monitor":
        return PASS, "this process is per-monitor DPI aware: coordinates are real pixels on every screen"
    if mode in ("system", "unaware"):
        return WARN, f"DPI awareness is '{mode}': on a scaled or mixed-scale setup Windows virtualises coordinates, so clicks and window rectangles can land off target"
    return WARN, "DPI awareness could not be read"


def check_monitors(env: Env) -> tuple[str, str, dict[str, Any]]:
    mons = env.monitors()
    if not mons:
        return FAIL, "no monitors were reported", {}
    info = {"count": len(mons), "monitors": [m.__dict__ for m in mons]}
    scales = sorted({round(m.scale, 2) for m in mons})
    notes = []
    if len(scales) > 1:
        notes.append(f"mixed scaling ({', '.join(f'{int(s * 100)}%' for s in scales)}) - the case where coordinates break first")
    if any(m.left < 0 or m.top < 0 for m in mons):
        notes.append("a monitor sits at negative coordinates (left of / above the primary) - handled only if the checks below pass")
    if sum(m.primary for m in mons) != 1:
        return FAIL, f"{sum(m.primary for m in mons)} primary monitors reported (expected exactly 1)", info
    summary = f"{len(mons)} monitor(s): " + ", ".join(f"#{m.index} {m.width}x{m.height}@{int(m.scale * 100)}%" for m in mons)
    return (WARN if notes else PASS), summary + ("; " + "; ".join(notes) if notes else ""), info


def check_capture_sizes(env: Env) -> tuple[str, str]:
    bad = []
    for m in env.monitors():
        got = env.capture_size(m)
        if abs(got[0] - m.width) > 1 or abs(got[1] - m.height) > 1:
            bad.append(f"#{m.index}: screen is {m.width}x{m.height} but the capture is {got[0]}x{got[1]}")
    return (FAIL, "capture size differs from the real screen (scaling is being applied to screenshots): " + "; ".join(bad)) if bad else \
        (PASS, "screenshots are the real physical size on every monitor")


# ------------------------------------------------------------------ checks that need Notepad (--launch)


def check_rect_agreement(env: Env, hwnd: int) -> tuple[str, str, dict[str, Any]]:
    r = env.rects(hwnd)
    evidence = {"visible": r.visible, "capture": r.capture, "uia": r.uia}
    problems = []
    if not _close(r.visible, r.capture):
        problems.append(f"window code {r.visible} vs capture {r.capture}")
    if r.uia is not None and not _close(r.visible, r.uia):
        problems.append(f"window code {r.visible} vs UI Automation {r.uia}")
    if problems:
        return FAIL, "the three sources disagree by more than " + f"{TOLERANCE_PX}px (a DPI/scaling mismatch): " + "; ".join(problems), evidence
    return (PASS if r.uia is not None else WARN), ("window rectangle agrees across window code, capture" + (" and UI Automation" if r.uia else "; UI Automation gave no rectangle")), evidence


def check_uia(env: Env, hwnd: int) -> tuple[str, str, dict[str, Any]]:
    try:
        count, ms, names = env.uia_tree(hwnd)
    except Exception as exc:
        return FAIL, f"UI Automation is unavailable: {type(exc).__name__}: {str(exc)[:120]}", {}
    ev = {"elements": count, "ms": ms, "sample": names[:8]}
    if count < UIA_MIN_ELEMENTS:
        return FAIL, f"only {count} UI elements found in Notepad (expected at least {UIA_MIN_ELEMENTS}): the accessibility tree is not readable", ev
    if ms > UIA_SLOW_MS:
        return WARN, f"{count} elements read, but it took {ms} ms (control search will feel slow)", ev
    return PASS, f"{count} elements read in {ms} ms", ev


def check_focus(env: Env, hwnd: int) -> tuple[str, str]:
    ok = env.focus(hwnd)
    time.sleep(0.15)
    front = env.foreground()
    if ok and front == hwnd:
        return PASS, "focus_window brought Notepad to the front and Windows agrees"
    return FAIL, f"focus_window returned {ok} and the foreground window is {front} (not Notepad {hwnd}): Windows blocked the focus change"


def check_each_monitor(env: Env, hwnd: int) -> list[Result]:
    out = []
    for m in env.monitors():
        def one(m: MonitorInfo = m):
            env.move_to(hwnd, m)
            time.sleep(0.25)
            vis = env.rects(hwnd).visible
            left, top, right, bottom = m.work if any(m.work) else (m.left, m.top, m.left + m.width, m.top + m.height)
            inside = vis[0] >= left - TOLERANCE_PX and vis[1] >= top - TOLERANCE_PX and vis[2] <= right + TOLERANCE_PX and vis[3] <= bottom + TOLERANCE_PX
            if inside:
                return PASS, f"moved to monitor #{m.index}; the window ({vis}) is inside that screen's work area {m.work or (m.left, m.top, right, bottom)}"
            return FAIL, f"asked to place the window on monitor #{m.index} but it is at {vis}, outside {(left, top, right, bottom)}"
        out.append(timed(f"window placement on monitor #{m.index}", one))
    return out


# ------------------------------------------------------------------ runner


def run(env: Env, *, launch: bool = False) -> Report:
    report = Report("windows")
    first = timed("platform", lambda: check_platform(env))
    report.add(first)
    if first.status == PENDING:
        return report
    report.environment = {"monitors": len(env.monitors()) if env.is_windows() else 0}
    report.add(timed("DPI awareness", lambda: check_dpi(env)))
    report.add(timed("monitors and scaling", lambda: check_monitors(env)))
    report.add(timed("screenshots match the real screen size", lambda: check_capture_sizes(env)))
    if not launch:
        for name in ("window rectangles agree", "UI Automation reads a real window", "window focus", "window placement per monitor"):
            report.add(Result(name, PENDING, "needs a real window: run again with --launch (opens Notepad, closes it afterwards)"))
        return report
    hwnd = 0
    try:
        hwnd = env.open_notepad()
        if not hwnd:
            for name in ("window rectangles agree", "UI Automation reads a real window", "window focus", "window placement per monitor"):
                report.add(Result(name, SKIP, "Notepad's window was not found after starting it"))
            return report
        report.add(timed("window rectangles agree", lambda: check_rect_agreement(env, hwnd)))
        report.add(timed("UI Automation reads a real window", lambda: check_uia(env, hwnd)))
        report.add(timed("window focus", lambda: check_focus(env, hwnd)))
        for r in check_each_monitor(env, hwnd):
            report.add(r)
    finally:
        if hwnd:
            try:
                env.close(hwnd)
            except Exception:
                pass
    return report


# ------------------------------------------------------------------ the real environment (Windows only; run by you)


@dataclass
class RealEnv(Env):
    notes: list[str] = field(default_factory=list)

    def is_windows(self) -> bool:
        from app.tools import winapi

        return winapi.is_windows()

    def dpi_awareness(self) -> str:
        import ctypes

        try:
            value = ctypes.c_int(-1)
            ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(value))
            return {0: "unaware", 1: "system", 2: "per-monitor"}.get(value.value, "unknown")
        except Exception:
            return "unknown"

    def monitors(self) -> list[MonitorInfo]:
        import ctypes

        import win32api

        from app.tools import winapi
        from app.vision.capture import physical_pixels

        work = {(m.area.left, m.area.top): m.area for m in winapi.monitors()}
        out = []
        with physical_pixels():
            raw = []
            for handle, _, rect in win32api.EnumDisplayMonitors():
                info = win32api.GetMonitorInfo(handle)
                left, top, right, bottom = info.get("Monitor", rect)
                dx, dy = ctypes.c_uint(96), ctypes.c_uint(96)
                try:
                    ctypes.windll.shcore.GetDpiForMonitor(int(handle), 0, ctypes.byref(dx), ctypes.byref(dy))
                except Exception:
                    pass
                raw.append((bool(info.get("Flags", 0) & 1), left, top, right - left, bottom - top, dx.value / 96.0))
        raw.sort(key=lambda m: (not m[0], m[1], m[2]))
        for i, (primary, left, top, w, h, scale) in enumerate(raw, start=1):
            area = work.get((left, top))
            out.append(MonitorInfo(i, left, top, w, h, primary, scale, (area.left, area.top, area.right, area.bottom) if area else (0, 0, 0, 0)))
        return out

    def capture_size(self, monitor: MonitorInfo) -> tuple[int, int]:
        from app.vision import capture as cap

        image = cap.backend.grab((monitor.left, monitor.top, monitor.left + monitor.width, monitor.top + monitor.height))
        return image.size

    def open_notepad(self) -> int:
        from app.tools import winapi

        before = {w.hwnd for w in winapi.visible_windows()}
        winapi.start_program("notepad.exe", [])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            for w in winapi.visible_windows():
                if w.hwnd not in before and "notepad" in (w.process_name or "").lower():
                    return int(w.hwnd)
            time.sleep(0.25)
        return 0

    def rects(self, hwnd: int) -> Rects:
        from app.tools import winapi
        from app.vision import capture as cap

        r = winapi.window_rect(hwnd)
        visible = (r.left, r.top, r.right, r.bottom)
        capture = tuple(cap.backend.window_rect(hwnd))
        uia = None
        try:
            from app.tools.browser_tabs import _automation

            found = _automation()
            if found is not None:
                b = found[0].ElementFromHandle(hwnd).CurrentBoundingRectangle
                uia = (b.left, b.top, b.right, b.bottom)
        except Exception:
            uia = None
        return Rects(visible, capture, uia)  # type: ignore[arg-type]

    def uia_tree(self, hwnd: int) -> tuple[int, int, list[str]]:
        from app.tools.browser_tabs import _automation

        found = _automation()
        if found is None:
            raise RuntimeError("the UI Automation library could not be loaded (comtypes / UIAutomationCore)")
        uia = found[0]
        started = time.perf_counter()
        items = uia.ElementFromHandle(hwnd).FindAll(4, uia.CreateTrueCondition())
        names = []
        for i in range(min(items.Length, 40)):
            name = str(items.GetElement(i).CurrentName or "")
            if name:
                names.append(name[:30])
        return int(items.Length), int((time.perf_counter() - started) * 1000), names

    def focus(self, hwnd: int) -> bool:
        from app.tools import winapi

        return bool(winapi.focus_window(hwnd))

    def foreground(self) -> int:
        from app.tools import winapi

        return winapi.foreground_window()

    def move_to(self, hwnd: int, monitor: MonitorInfo) -> None:
        from app.tools import winapi

        area = monitor.work if any(monitor.work) else (monitor.left, monitor.top, monitor.left + monitor.width, monitor.top + monitor.height)
        w, h = (area[2] - area[0]) // 2, (area[3] - area[1]) // 2
        winapi.move_window(hwnd, winapi.Rect(area[0] + 40, area[1] + 40, area[0] + 40 + w, area[1] + 40 + h))

    def close(self, hwnd: int) -> None:
        from app.tools import winapi

        winapi.close_window(hwnd)
