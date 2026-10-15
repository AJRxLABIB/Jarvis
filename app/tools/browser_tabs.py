"""Reuse one browser tab for one kind of page (YouTube: songs always play in the same tab).

"Play X" twice used to open two YouTube tabs. Now Jarvis first looks for a tab that already shows
the site, in the browser it is about to use:

  1. a browser window whose title shows the site (the site's tab is the active one there), else
  2. the browser's tab strip, read with Windows UI Automation (the accessibility API screen readers
     use): a tab titled with the site is selected, else
  3. only if UI Automation isn't available: the browser's own tab search (Ctrl+Shift+A).

When such a tab is found and really is in front, the address bar gets the new URL (Ctrl+L, the URL,
Enter). Keys are only ever sent after checking that the browser window has the focus, so they can
never land in another app. When no tab is found, the caller opens a new tab as before. Everything
else Jarvis opens (Facebook, Gmail, ...) keeps opening in a new tab of the same browser and profile.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any

from app.tools import winapi
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.browser_tabs")

# the word a site's tabs carry in their title ("Song name - YouTube")
TAB_MARKS: dict[str, str] = {"youtube": "YouTube"}
TAB_BROWSERS = frozenset({"chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "opera.exe", "vivaldi.exe"})
_TAB_SEARCH = frozenset({"chrome.exe", "msedge.exe", "brave.exe"})  # Ctrl+Shift+A = search your tabs


def has_mark(title: str, mark: str) -> bool:
    """'Song - YouTube - Google Chrome' has 'YouTube'; a page that merely mentions it in the middle doesn't count
    unless it is the site's own suffix (" - YouTube") or the whole title."""
    text = title.strip()
    lowered, key = text.lower(), mark.lower()
    return (lowered == key or f" - {key}" in lowered or f" – {key}" in lowered or f" — {key}" in lowered
            or lowered.startswith(f"{key} - ") or lowered.startswith(f"({key})"))


def browser_windows(exe: str) -> list[winapi.WindowInfo]:
    try:
        return [w for w in winapi.visible_windows() if w.process_name == exe.lower()]
    except Exception:
        return []


# ------------------------------------------------------------------ UI Automation (tab strip)

_UIA_FAILED = False
_THREAD = threading.local()  # COM objects belong to the thread that made them
UIA_TAB_CONTROL, UIA_TAB_ITEM = 50018, 50019
UIA_BUTTON, UIA_DOCUMENT = 50000, 50030
UIA_CONTROL_TYPE_PROPERTY = 30003
UIA_INVOKE_PATTERN, UIA_SELECTION_ITEM_PATTERN, UIA_LEGACY_PATTERN = 10000, 10010, 10018
SCOPE_CHILDREN, SCOPE_DESCENDANTS = 2, 4


def _automation() -> tuple[Any, Any] | None:
    global _UIA_FAILED
    if _UIA_FAILED or not winapi.is_windows():
        return None
    cached = getattr(_THREAD, "uia", None)
    if cached is not None:
        return cached
    try:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        module = comtypes.client.GetModule("UIAutomationCore.dll")
        _THREAD.uia = (comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}",
                                                    interface=module.IUIAutomation), module)
        return _THREAD.uia
    except Exception:
        _UIA_FAILED = True
        _log.info("UI Automation isn't available; tab reuse falls back to the browser's tab search", exc_info=True)
        return None


def tab_items(hwnd: int) -> list[tuple[str, Any]] | None:
    """(title, element) of every tab in a browser window; None when UI Automation can't be used."""
    found = _automation()
    if found is None:
        return None
    uia, _ = found
    try:
        window = uia.ElementFromHandle(hwnd)
        strip = window.FindFirst(SCOPE_DESCENDANTS, uia.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY,
                                                                                UIA_TAB_CONTROL))
        if not strip:
            return []
        items = strip.FindAll(SCOPE_DESCENDANTS, uia.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY, UIA_TAB_ITEM))
        return [(str(items.GetElement(i).CurrentName or ""), items.GetElement(i)) for i in range(items.Length)]
    except Exception:
        _log.info("could not read the browser's tabs", exc_info=True)
        return None


def select_tab(element: Any) -> bool:
    found = _automation()
    if found is None:
        return False
    _, module = found
    for pattern_id, interface, action in (
        (UIA_SELECTION_ITEM_PATTERN, "IUIAutomationSelectionItemPattern", "Select"),
        (UIA_LEGACY_PATTERN, "IUIAutomationLegacyIAccessiblePattern", "DoDefaultAction"),
    ):
        try:
            pattern = element.GetCurrentPattern(pattern_id)
            if pattern:
                getattr(pattern.QueryInterface(getattr(module, interface)), action)()
                return True
        except Exception:
            continue
    try:  # last resort: click the middle of the tab
        rect = element.CurrentBoundingRectangle
        if rect.right > rect.left and rect.bottom > rect.top:
            winapi.click_at((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            return True
    except Exception:
        pass
    return False


def _document(hwnd: int) -> Any | None:
    found = _automation()
    if found is None:
        return None
    uia, _ = found
    try:
        window = uia.ElementFromHandle(hwnd)
        return window.FindFirst(SCOPE_DESCENDANTS, uia.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY,
                                                                               UIA_DOCUMENT)) or None
    except Exception:
        return None


def focus_page(hwnd: int) -> bool:
    """Put the keyboard focus on the web page (not the address bar), so YouTube's keys (k, l, j...) work."""
    page = _document(hwnd)
    if page is None:
        return False
    try:
        page.SetFocus()
        return True
    except Exception:
        return False


def page_buttons(hwnd: int) -> list[tuple[str, Any]]:
    """(name, element) of the buttons on the page in front of this browser window."""
    found = _automation()
    page = _document(hwnd)
    if found is None or page is None:
        return []
    uia, _ = found
    try:
        items = page.FindAll(SCOPE_DESCENDANTS, uia.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY, UIA_BUTTON))
        return [(str(items.GetElement(i).CurrentName or ""), items.GetElement(i)) for i in range(min(items.Length, 800))]
    except Exception:
        return []


def press_button(element: Any) -> bool:
    found = _automation()
    if found is None:
        return False
    _, module = found
    try:
        pattern = element.GetCurrentPattern(UIA_INVOKE_PATTERN)
        if pattern:
            pattern.QueryInterface(module.IUIAutomationInvokePattern).Invoke()
            return True
    except Exception:
        pass
    return select_tab(element)  # the same fallbacks: default action, then a click in its middle


def click_page_button(hwnd: int, pattern: str) -> str | None:
    """Press the first page button whose name matches ``pattern`` (a regex); its name, or None."""
    wanted = re.compile(pattern, re.I)
    for name, element in page_buttons(hwnd):
        if name and wanted.search(name) and press_button(element):
            return name
    return None


def site_titles(exe: str, site: str) -> list[str]:
    """Titles of the site's tabs in this browser, read without switching anything."""
    mark = TAB_MARKS.get(site)
    if mark is None:
        return []
    titles: list[str] = []
    for window in browser_windows(exe):
        tabs = tab_items(window.hwnd)
        if tabs is None:
            names = [window.title]
        else:
            names = [title for title, _ in tabs] or [window.title]
        titles += [t for t in names if has_mark(t, mark)]
    return titles


# ------------------------------------------------------------------ finding and using the tab


def _wait(check: Callable[[], bool], seconds: float, sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.08)


def _title_has(hwnd: int, mark: str) -> bool:
    try:
        return has_mark(winapi.window_title(hwnd), mark)
    except Exception:
        return False


def _in_front(hwnd: int) -> bool:
    return winapi.foreground_window() == hwnd


def _bring(hwnd: int) -> bool:
    winapi.force_foreground(hwnd)
    return _wait(lambda: _in_front(hwnd), 0.8)


def find_site_tab(exe: str, site: str, prefer: Callable[[winapi.WindowInfo], bool] | None = None) -> int | None:
    """Put the site's existing tab in front and return its window, or None if there is none."""
    mark = TAB_MARKS.get(site)
    exe = exe.lower()
    if mark is None or exe not in TAB_BROWSERS or not winapi.is_windows():
        return None
    windows = browser_windows(exe)
    if not windows:
        return None
    if prefer is not None:
        windows.sort(key=lambda w: not prefer(w))
    showing = [w for w in windows if has_mark(w.title, mark)]
    if showing:
        return showing[0].hwnd if _bring(showing[0].hwnd) else None
    uia_worked = False
    for window in windows:
        tabs = tab_items(window.hwnd)
        if tabs is None:
            continue
        uia_worked = True
        match = next((element for title, element in tabs if has_mark(title, mark)), None)
        if match is None:
            continue
        if not _bring(window.hwnd):
            return None
        if select_tab(match) and _wait(lambda h=window.hwnd: _title_has(h, mark), 1.0):
            return window.hwnd
        return None
    if uia_worked or exe not in _TAB_SEARCH:
        return None  # the tab strips were read: there is no such tab
    return _tab_search(windows[0].hwnd, mark)


def _tab_search(hwnd: int, mark: str) -> int | None:
    """Without UI Automation: the browser's own "Search tabs" box, then check the title really changed."""
    if not _bring(hwnd):
        return None
    winapi.press_combo("ctrl+shift+a")
    time.sleep(0.45)
    if not _in_front(hwnd):
        return None
    winapi.type_unicode(mark)
    time.sleep(0.35)
    winapi.press_combo("enter")
    if _wait(lambda: _title_has(hwnd, mark), 1.0):
        return hwnd
    if _in_front(hwnd):
        winapi.press_combo("esc")  # nothing matched: close the search box
    return None


def navigate(hwnd: int, url: str) -> bool:
    """Type ``url`` into the address bar of the tab in front. Refuses when that window isn't in front."""
    if not _in_front(hwnd) and not _bring(hwnd):
        return False
    winapi.press_combo("esc")  # leave video full screen / close a pop-up; harmless otherwise
    time.sleep(0.05)
    if not _in_front(hwnd):
        return False
    winapi.press_combo("ctrl+l")
    time.sleep(0.12)
    if not _in_front(hwnd):
        return False
    winapi.type_unicode(url)
    winapi.press_combo("enter")
    return True


def reuse_tab(exe: str, site: str, url: str | None, step_aside: Callable[[], None] | None = None,
              prefer: Callable[[winapi.WindowInfo], bool] | None = None) -> bool:
    """Show (and with ``url``, load) the site's existing tab. False = no such tab, open a new one."""
    if TAB_MARKS.get(site) is None or exe.lower() not in TAB_BROWSERS or not browser_windows(exe):
        return False
    if step_aside is not None and winapi.foreground_is_ours():
        try:
            step_aside()  # Jarvis's window fades away first, so it can't take the keys
        except Exception:
            pass
    try:
        hwnd = find_site_tab(exe, site, prefer)
        if hwnd is None:
            return False
        if url is not None and not navigate(hwnd, url):
            return False
    except Exception:
        _log.warning("could not reuse the browser tab", exc_info=True)
        return False
    log_event(_log, "tab_reused", site=site, browser=exe)
    return True
