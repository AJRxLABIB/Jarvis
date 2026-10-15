"""Window layouts by voice: snap, side by side, other screen, maximize all, and saved layouts.

"put Chrome on the left", "move this window to the right", "Chrome left and VS Code right", "move Chrome to
my second screen", "maximize everything", "save this layout as work", "work layout" / "restore my work
layout", "list my layouts". A saved layout remembers which apps were open and where (program path, window
title, position) in layouts.json in the data folder; restoring moves the open windows back and starts the
apps that aren't open. Nothing is typed or clicked: windows are moved with the Windows window API.
"""

from __future__ import annotations

import json
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.layouts")

LAYOUT_FILE = "layouts.json"
PLACES = ("left", "right", "top", "bottom", "top_left", "top_right", "bottom_left", "bottom_right", "center",
          "maximize")
_SKIP_PROCESSES = {"jarvis.exe", "jarvis-cli.exe", "textinputhost.exe", "shellexperiencehost.exe",
                   "searchhost.exe", "startmenuexperiencehost.exe", "lockapp.exe"}
_NAME = re.compile(r"^[\w][\w .'-]{0,39}$", re.UNICODE)


class LayoutArgs(ToolArgs):
    action: Literal["left", "right", "top", "bottom", "top_left", "top_right", "bottom_left", "bottom_right",
                    "center", "maximize", "next_screen", "side_by_side", "maximize_all", "save", "restore",
                    "list", "delete"] = Field(description="Where to put the window, or what to do with a saved layout")
    app: str | None = Field(default=None, max_length=64,
                            description="The app to move; empty = the window you are using now")
    second_app: str | None = Field(default=None, max_length=64, description="For side_by_side: the app on the right")
    layout: str | None = Field(default=None, max_length=40, description="For save/restore/delete: the layout's name")


# ------------------------------------------------------------------ geometry


def place(area: winapi.Rect, where: str) -> winapi.Rect:
    """The part of a screen's work area for 'left', 'top_right', 'center'..."""
    half_w, half_h = area.width // 2, area.height // 2
    x0, t, r, b = area.left, area.top, area.right, area.bottom
    mid_x, mid_y = x0 + half_w, t + half_h
    boxes = {
        "left": (x0, t, mid_x, b), "right": (mid_x, t, r, b), "top": (x0, t, r, mid_y), "bottom": (x0, mid_y, r, b),
        "top_left": (x0, t, mid_x, mid_y), "top_right": (mid_x, t, r, mid_y),
        "bottom_left": (x0, mid_y, mid_x, b), "bottom_right": (mid_x, mid_y, r, b),
    }
    if where == "center":
        w, h = area.width * 7 // 10, area.height * 8 // 10
        return winapi.Rect(x0 + (area.width - w) // 2, t + (area.height - h) // 2, x0 + (area.width + w) // 2,
                           t + (area.height + h) // 2)
    if where == "maximize":
        return area
    return winapi.Rect(*boxes[where])


def screen_of(rect: winapi.Rect, screens: list[winapi.Monitor]) -> int:
    """Index of the screen holding the middle of ``rect`` (the primary one if none does)."""
    x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    for index, screen in enumerate(screens):
        a = screen.area
        if a.left <= x < a.right and a.top <= y < a.bottom:
            return index
    return 0


def moved_to(rect: winapi.Rect, source: winapi.Rect, target: winapi.Rect) -> winapi.Rect:
    """The same relative position and size on another screen."""
    sx, sy = target.width / max(1, source.width), target.height / max(1, source.height)
    left = target.left + round((rect.left - source.left) * sx)
    top = target.top + round((rect.top - source.top) * sy)
    width = min(target.width, round(rect.width * sx))
    height = min(target.height, round(rect.height * sy))
    return winapi.Rect(left, top, left + width, top + height)


# ------------------------------------------------------------------ finding windows


def _mine(window: winapi.WindowInfo) -> bool:
    return window.pid == os.getpid() or window.process_name in _SKIP_PROCESSES


def _window_for(app: str | None, ctx: ToolContext) -> tuple[str, winapi.WindowInfo]:
    if app is None or app.strip().lower() in ("", "this", "this window", "current", "it", "that"):
        target = winapi.target_window()
        if target is None:
            raise ToolError("Which window? I don't see one to move.", "missing_target")
        return (target.title or "the window")[:60], target
    from app.tools.applications import _target_windows

    display, windows, _ = _target_windows(app, ctx)
    windows = [w for w in windows if not _mine(w)]
    if not windows:
        raise ToolError(f"{display} doesn't seem to be open.", "not_running")
    return display, windows[0]


def _put(window: winapi.WindowInfo, where: str) -> None:
    screens = winapi.monitors()
    if not screens:
        raise ToolError("Windows didn't report any screen.", "no_screen")
    if where == "maximize":
        winapi.show_window(window.hwnd, winapi.SW_MAXIMIZE)
        return
    index = screen_of(winapi.window_rect(window.hwnd), screens)
    winapi.move_window(window.hwnd, place(screens[index].area, where))


_WORDS = {"left": "on the left", "right": "on the right", "top": "at the top", "bottom": "at the bottom",
          "top_left": "in the top left corner", "top_right": "in the top right corner",
          "bottom_left": "in the bottom left corner", "bottom_right": "in the bottom right corner",
          "center": "in the middle", "maximize": "full screen"}


# ------------------------------------------------------------------ saved layouts


def _file(ctx: ToolContext) -> Path | None:
    return (ctx.paths.data_dir / LAYOUT_FILE) if ctx.paths is not None else None


def load_layouts(ctx: ToolContext) -> dict[str, dict]:
    path = _file(ctx)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(ctx: ToolContext, layouts: dict[str, dict]) -> None:
    path = _file(ctx)
    if path is None:
        raise ToolError("There's no data folder to save layouts in.", "no_data_dir")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(layouts, indent=1, ensure_ascii=False), encoding="utf-8")


def layout_key(name: str | None) -> str:
    text = " ".join((name or "").lower().replace("layout", " ").split()).strip(" .'")
    if not text or not _NAME.match(text):
        raise ToolError("What should the layout be called? For example 'work' or 'coding'.", "missing_name")
    return text


def snapshot() -> list[dict]:
    """The windows on screen now (not minimized, not Jarvis), in the order Windows lists them (front first)."""
    entries = []
    for window in winapi.visible_windows():
        if _mine(window) or window.minimized or not window.title:
            continue
        rect = winapi.window_rect(window.hwnd)
        if rect.width < 80 or rect.height < 60:
            continue
        entries.append({"process": window.process_name, "path": winapi.process_path(window.pid) or "",
                        "title": window.title[:120], "rect": [rect.left, rect.top, rect.right, rect.bottom],
                        "maximized": winapi.window_state(window.hwnd) == "maximized"})
    return entries


def _best(entry: dict, windows: list[winapi.WindowInfo], used: set[int]) -> winapi.WindowInfo | None:
    same = [w for w in windows if w.process_name == entry.get("process") and w.hwnd not in used]
    if not same:
        return None
    return max(same, key=lambda w: SequenceMatcher(None, w.title.lower(), str(entry.get("title", "")).lower()).ratio())


def _can_start(path: str) -> bool:
    return bool(path) and path.lower().endswith(".exe") and PureWindowsPath(path).name.lower() not in _SKIP_PROCESSES \
        and Path(path).is_file()


def restore(entries: list[dict], wait_seconds: float = 12.0, sleep=time.sleep) -> tuple[int, list[str], list[str]]:
    """Move windows back; start missing apps. Returns (moved, started, missing)."""
    windows = [w for w in winapi.visible_windows() if not _mine(w)]
    used: set[int] = set()
    started: list[str] = []
    missing: list[str] = []
    pending: list[dict] = []
    moved = 0
    for entry in entries:
        window = _best(entry, windows, used)
        if window is not None:
            used.add(window.hwnd)
            _apply(window, entry)
            moved += 1
            continue
        path = str(entry.get("path", ""))
        if _can_start(path) and PureWindowsPath(path).name.lower() not in {PureWindowsPath(p["path"]).name.lower()
                                                                           for p in pending if p.get("path")}:
            winapi.start_program(path, [])
            started.append(PureWindowsPath(path).stem)
        pending.append(entry)
    deadline = time.monotonic() + (wait_seconds if started else 0)
    while pending and time.monotonic() < deadline:
        sleep(0.5)
        windows = [w for w in winapi.visible_windows() if not _mine(w)]
        for entry in list(pending):
            window = _best(entry, windows, used)
            if window is not None:
                used.add(window.hwnd)
                _apply(window, entry)
                moved += 1
                pending.remove(entry)
    missing = [str(e.get("process", "?")).removesuffix(".exe") for e in pending]
    return moved, started, missing


def _apply(window: winapi.WindowInfo, entry: dict) -> None:
    left, top, right, bottom = (int(v) for v in entry["rect"])
    if entry.get("maximized"):
        winapi.move_window(window.hwnd, winapi.Rect(left, top, right, bottom))  # the right screen first
        winapi.show_window(window.hwnd, winapi.SW_MAXIMIZE)
    else:
        winapi.move_window(window.hwnd, winapi.Rect(left, top, right, bottom))


# ------------------------------------------------------------------ the tool


def arrange_windows(args: LayoutArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    action = args.action
    if action in PLACES:
        display, window = _window_for(args.app, ctx)
        _put(window, action)
        return ToolResult.success(f"Put {display} {_WORDS[action]}.", app=display)
    if action == "side_by_side":
        if not args.app or not args.second_app:
            raise ToolError("Which two apps should go side by side?", "missing_target")
        first, left = _window_for(args.app, ctx)
        second, right = _window_for(args.second_app, ctx)
        if left.hwnd == right.hwnd:
            raise ToolError(f"{first} and {second} are the same window.", "same_window")
        _put(left, "left")
        _put(right, "right")
        return ToolResult.success(f"{first} on the left, {second} on the right.")
    if action == "next_screen":
        display, window = _window_for(args.app, ctx)
        screens = winapi.monitors()
        if len(screens) < 2:
            return ToolResult.failure("one_screen", "There's only one screen connected.")
        maximized = winapi.window_state(window.hwnd) == "maximized"
        rect = winapi.window_rect(window.hwnd)
        here = screen_of(rect, screens)
        there = (here + 1) % len(screens)
        winapi.move_window(window.hwnd, moved_to(rect, screens[here].area, screens[there].area))
        if maximized:
            winapi.show_window(window.hwnd, winapi.SW_MAXIMIZE)
        return ToolResult.success(f"Moved {display} to screen {there + 1}.", app=display, screen=there + 1)
    if action == "maximize_all":
        windows = [w for w in winapi.visible_windows() if not _mine(w) and not w.minimized]
        for window in windows:
            winapi.show_window(window.hwnd, winapi.SW_MAXIMIZE)
        return ToolResult.success(f"Maximized {len(windows)} window(s).", windows=len(windows))
    layouts = load_layouts(ctx)
    if action == "list":
        if not layouts:
            return ToolResult.success("You have no saved layouts. Say 'save this layout as work'.", layouts=[])
        return ToolResult.success(f"Your layouts: {', '.join(sorted(layouts))}.", layouts=sorted(layouts))
    key = layout_key(args.layout)
    if action == "save":
        entries = snapshot()
        if not entries:
            return ToolResult.failure("nothing_open", "There are no windows open to save.")
        layouts[key] = {"saved": ctx.clock().isoformat(timespec="seconds"), "windows": entries}
        _store(ctx, layouts)
        log_event(_log, "layout_saved", windows=len(entries))
        names = sorted({e["process"].removesuffix(".exe") for e in entries})
        return ToolResult.success(f"Saved the '{key}' layout with {len(entries)} window(s): {', '.join(names)}. "
                                  f"Say '{key} layout' to bring it back.", windows=len(entries))
    if key not in layouts:
        known = f" Your layouts: {', '.join(sorted(layouts))}." if layouts else ""
        return ToolResult.failure("not_found", f"There's no layout called '{key}'.{known}")
    if action == "delete":
        del layouts[key]
        _store(ctx, layouts)
        return ToolResult.success(f"Deleted the '{key}' layout.")
    entries = [e for e in layouts[key].get("windows", []) if isinstance(e, dict) and len(e.get("rect", [])) == 4]
    if ctx.step_aside is not None and winapi.foreground_is_ours():
        try:
            ctx.step_aside()
        except Exception:
            pass
    moved, started, missing = restore(list(reversed(entries)))  # back to front, so the front window ends on top
    message = f"Restored the '{key}' layout: {moved} window(s) in place."
    if started:
        message += f" Started {', '.join(started)}."
    if missing:
        message += f" Couldn't find {', '.join(missing)}."
    return ToolResult.success(message, moved=moved, started=started, missing=missing)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="arrange_windows",
        description="Arrange windows: put an app (or the current window) on the left/right/top/bottom half, a "
                    "corner, the middle or full screen; two apps side by side; move a window to the next screen; "
                    "maximize all; save the current window layout under a name, restore/list/delete saved layouts.",
        handler=arrange_windows, args_model=LayoutArgs, level=RiskLevel.SAFE, category="apps", timeout=40,
    ),
)
