"""Pressing buttons by name through Windows UI Automation (accessibility-first computer use).

"press Save", "click OK", "click the Settings tab", "tick Remember me" -> the named control in the window
you're using is found in the accessibility tree (what screen readers use) and pressed with its own action:
Invoke for buttons and links, Toggle for check boxes, Select for tabs and list items, Expand for menus.
Only when a control has none of these is its middle clicked.

Why first: it doesn't need screen capture or OCR, works when text is small or the window is partly covered,
and never clicks a similar-looking word somewhere else. When nothing matches, Jarvis says so (and "click
the text X" still uses OCR).

Verification: after pressing, Jarvis looks for a real reaction - the control's own state (a check box is now
ticked), the control or its window gone (a dialog closed), a new window, or a changed title. If nothing
visible changed it says "I pressed it, but I can't see a change" instead of claiming success.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.ui_elements")

# UIA control type ids -> a word for messages
PRESSABLE = {50000: "button", 50005: "link", 50002: "check box", 50013: "radio button", 50019: "tab",
             50007: "list item", 50024: "tree item", 50011: "menu item", 50031: "split button", 50003: "combo box"}
PATTERN_INVOKE, PATTERN_TOGGLE, PATTERN_SELECTION_ITEM, PATTERN_EXPAND, PATTERN_LEGACY = 10000, 10015, 10010, 10005, 10018
PROP_CONTROL_TYPE, PROP_IS_ENABLED, PROP_IS_OFFSCREEN = 30003, 30010, 30022
SCOPE_DESCENDANTS = 4
MAX_ELEMENTS = 1500


@dataclass
class Found:
    name: str
    kind: str
    element: Any
    score: float


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[&_:.…]+", " ", text.lower()).split())


def score(wanted: str, name: str) -> float:
    """1.0 exact, 0.9 whole words at the start ("Save" in "Save as"), 0.7 contained, else 0."""
    w, n = _norm(wanted), _norm(name)
    if not w or not n:
        return 0.0
    negation = re.compile(r"\b(don'?t|do not|not|no|never|cancel)\b")
    if negation.search(n) and not negation.search(w):
        return 0.0  # "save" must never press "Don't save"
    if n == w:
        return 1.0
    if n.startswith(w + " ") or n.endswith(" " + w):
        return 0.9
    if f" {w} " in f" {n} ":
        return 0.75
    return 0.0


def find_controls(hwnd: int, wanted: str) -> list[Found] | None:
    """Pressable controls in the window whose name matches, best first; None = UI Automation unavailable."""
    from app.tools.browser_tabs import _automation

    found = _automation()
    if found is None:
        return None
    uia, _ = found
    try:
        root = uia.ElementFromHandle(hwnd)
        conditions = [uia.CreatePropertyCondition(PROP_CONTROL_TYPE, cid) for cid in PRESSABLE]
        condition = conditions[0]
        for extra in conditions[1:]:
            condition = uia.CreateOrCondition(condition, extra)
        condition = uia.CreateAndCondition(condition, uia.CreatePropertyCondition(PROP_IS_ENABLED, True))
        items = root.FindAll(SCOPE_DESCENDANTS, condition)
        matches = []
        for i in range(min(items.Length, MAX_ELEMENTS)):
            element = items.GetElement(i)
            name = str(element.CurrentName or "")
            points = score(wanted, name)
            if points and not element.CurrentIsOffscreen:
                matches.append(Found(name, PRESSABLE.get(element.CurrentControlType, "control"), element, points))
        return sorted(matches, key=lambda f: -f.score)
    except Exception:
        _log.info("UI Automation search failed", exc_info=True)
        return None


def _toggle_state(element: Any) -> int | None:
    from app.tools.browser_tabs import _automation

    found = _automation()
    if found is None:
        return None
    try:
        pattern = element.GetCurrentPattern(PATTERN_TOGGLE)
        return int(pattern.QueryInterface(found[1].IUIAutomationTogglePattern).CurrentToggleState) if pattern else None
    except Exception:
        return None


def press(element: Any) -> str:
    """Use the control's own action; returns which one."""
    from app.tools.browser_tabs import _automation, select_tab

    found = _automation()
    if found is None:
        raise ToolError("UI Automation isn't available.", "unavailable")
    module = found[1]
    for pattern_id, interface, method, label in (
        (PATTERN_INVOKE, "IUIAutomationInvokePattern", "Invoke", "invoke"),
        (PATTERN_TOGGLE, "IUIAutomationTogglePattern", "Toggle", "toggle"),
        (PATTERN_SELECTION_ITEM, "IUIAutomationSelectionItemPattern", "Select", "select"),
        (PATTERN_EXPAND, "IUIAutomationExpandCollapsePattern", "Expand", "expand"),
    ):
        try:
            pattern = element.GetCurrentPattern(pattern_id)
            if pattern:
                getattr(pattern.QueryInterface(getattr(module, interface)), method)()
                return label
        except Exception:
            continue
    if select_tab(element):  # default action, then a click in its middle
        return "click"
    raise ToolError("That control can't be pressed.", "not_pressable")


def _snapshot() -> tuple[set[int], dict[int, str]]:
    windows = winapi.visible_windows()
    return {w.hwnd for w in windows}, {w.hwnd: w.title for w in windows}


def reacted(before: tuple[set[int], dict[int, str]], element: Any, toggle_before: int | None, wait: float = 1.2,
            sleep=time.sleep) -> str | None:
    """What changed after pressing (None = nothing visible)."""
    deadline = time.monotonic() + wait
    while True:
        if toggle_before is not None:
            now = _toggle_state(element)
            if now is not None and now != toggle_before:
                return "its state changed"
        hwnds, titles = _snapshot()
        if hwnds - before[0]:
            return "a new window opened"
        if before[0] - hwnds:
            return "a window closed"
        if any(titles.get(h) != t for h, t in before[1].items() if h in hwnds):
            return "the window changed"
        try:
            if element.CurrentIsOffscreen:
                return "it went away"
        except Exception:
            return "it went away"
        if time.monotonic() >= deadline:
            return None
        sleep(0.15)


class PressArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=80, description="The button, link, tab or check box, as labelled, "
                                                                "e.g. 'Save', 'OK', 'Remember me'")


def press_control(args: PressArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    from app.tools.screen_agent import hand_over

    hand_over(ctx)  # Jarvis's window steps aside: the control is in YOUR app
    window = winapi.target_window()
    if window is None:
        raise ToolError("Which window? I don't see one in front.", "no_window")
    from app.core import app_profiles

    process = str(getattr(window, "process_name", "") or "")
    matches = find_controls(window.hwnd, args.name)
    tried = [args.name]
    if matches == []:                                     # this program may label it differently ("Save" -> "Save As")
        for other in app_profiles.alternatives(process, args.name):
            tried.append(other)
            matches = find_controls(window.hwnd, other)
            if matches:
                break
    if matches is None:
        raise ToolError("I can't read this window's controls (UI Automation isn't available). Say 'click the text "
                        f"{args.name}' to find it on the screen instead.", "unavailable")
    if not matches:
        hint = app_profiles.advice(process, args.name)
        raise ToolError(f"I don't see a control called '{args.name}' in {window.title[:60] or 'this window'}"
                        + (f" (also tried: {', '.join(tried[1:])})" if len(tried) > 1 else "") + f". Say 'click the text {args.name}' to look for it on "
                        f"the screen. {hint}".rstrip(), "not_found")
    best = matches[0]
    if len(matches) > 1 and matches[1].score == best.score and _norm(matches[1].name) == _norm(best.name):
        pass  # identical duplicates (toolbar + menu): the first is fine
    before, toggle_before = _snapshot(), _toggle_state(best.element)
    how = press(best.element)
    change = reacted(before, best.element, toggle_before)
    log_event(_log, "control_pressed", kind=best.kind, how=how, verified=change is not None)
    if change is None:
        return ToolResult.success(f"I pressed the {best.kind} '{best.name}', but I can't see a change yet.",
                                  control=best.name, verified=None)
    return ToolResult.success(f"Pressed the {best.kind} '{best.name}' ({change}).", control=best.name, verified=True)


TOOLS: tuple[Tool, ...] = (
    Tool("press_control", "Press a button, link, tab, menu item or check box BY ITS NAME in the window the user is "
         "using (Windows accessibility, no screen reading): 'press Save', 'click OK', 'tick Remember me'. Prefer this "
         "over click_text for named controls.", press_control, PressArgs, RiskLevel.MODERATE, category="screen",
         timeout=30, confirmation=lambda a, c: f"Press '{a.name}' in the window you're using?"),
)
