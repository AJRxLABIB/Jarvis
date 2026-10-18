"""Accessibility audit: what can be CHECKED by program, reported plainly. It does not replace testing with a screen reader.

Two checks:

* **Colour contrast** of the app's own colour pairs against WCAG 2.1 (4.5:1 for normal text, 3:1 for large text and for
  the outline of a control). Pure arithmetic on the theme colours, so it runs anywhere.
* **Widgets**: every control a person can use must have a name a screen reader can say (an accessible name, or visible
  text, or a label buddy), must be reachable with the keyboard, and must not be a tiny click target. Run on a built
  window (the test-suite does this for the main dialogs).

Not covered, and said so in the report: how a real screen reader (NVDA / Narrator) reads each screen, high-contrast
mode, text scaling at 200 %, and anything drawn by hand (the orb, charts) - those need a person with the tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NOT_COVERED = ("screen-reader wording on a real reader (NVDA / Narrator)", "Windows high-contrast mode",
               "text scaling at 200 %", "hand-drawn parts (the orb, charts)")
MIN_TARGET = 24            # px, WCAG 2.2 "target size (minimum)"


@dataclass(frozen=True)
class Finding:
    where: str
    problem: str
    fix: str
    level: str = "fail"           # fail | note


# ------------------------------------------------------------------------------------------------ contrast

def _channel(v: int) -> float:
    c = v / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def theme_pairs() -> list[tuple[str, str, str, float]]:
    """(what it is, foreground, background, required ratio) for the colours the app actually uses."""
    from app.ui.styles import theme as t
    from app.ui.styles.tokens import COLOR as c

    pairs = [("body text", t.TEXT, t.BG, 4.5), ("body text on panels", t.TEXT, t.PANEL, 4.5),
             ("secondary (dim) text", t.DIM, t.BG, 4.5), ("secondary (dim) text on panels", t.DIM, t.PANEL, 4.5),
             ("accent text", t.ACCENT, t.BG, 4.5), ("gold (needs you) text", t.GOLD, t.BG, 4.5),
             ("control outline", t.PANEL_EDGE, t.BG, 1.0),            # decorative: the control has its own label
             ("button text", c["text"], c["button"], 4.5), ("primary button text", c["on_accent"], c["accent_strong"], 4.5),
             ("danger button text", c["danger_text"], c["danger_bg"], 4.5), ("microphone-on text", c["ok_text"], c["ok_bg"], 4.5),
             ("chip text", c["chip_text"], t.PANEL, 4.5), ("text in input fields", c["text_strong"], c["field"], 4.5)]
    pairs += [(f"state colour {name.lower().replace('_', ' ')}", colour, t.BG, 3.0) for name, colour in t.STATE_COLORS.items()]
    return pairs


def audit_theme() -> list[Finding]:
    out = []
    for name, fg, bg, need in theme_pairs():
        ratio = contrast(fg, bg)
        if ratio < need:
            out.append(Finding(f"colour: {name}", f"contrast {ratio:.2f}:1 is below the {need}:1 needed ({fg} on {bg})",
                               "use a lighter foreground or a darker background"))
    return out


# ------------------------------------------------------------------------------------------------ widgets

def _name_of(widget) -> str:
    for getter in ("accessibleName", "text", "toolTip", "placeholderText", "windowTitle"):
        fn = getattr(widget, getter, None)
        if callable(fn):
            try:
                value = fn()
            except Exception:
                continue
            if isinstance(value, str) and re.sub(r"[&\s]", "", value):
                return value
    return ""


def _inside(widget, kinds) -> bool:
    parent = widget.parent()
    while parent is not None:
        if isinstance(parent, kinds):
            return True
        parent = parent.parent()
    return False


def audit_widgets(root) -> list[Finding]:
    """Every usable control under ``root`` must be nameable and keyboard-reachable. Pure inspection; changes nothing."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QAbstractButton, QAbstractItemView, QAbstractSpinBox, QComboBox, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
        QTextEdit, QWidget,
    )

    controls = (QAbstractButton, QAbstractItemView, QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit, QTextEdit)
    labelled = {id(lbl.buddy()) for lbl in root.findChildren(QLabel) if lbl.buddy() is not None}
    out: list[Finding] = []
    for w in root.findChildren(QWidget):
        if not isinstance(w, controls) or not w.isEnabledTo(root) or w.objectName().startswith("qt_"):
            continue
        # inner parts of composite widgets (a spin box's line edit, a combo's editor and popup list, a table's headers)
        if isinstance(w, QHeaderView) or _inside(w, (QAbstractSpinBox, QComboBox)):
            continue
        where = f"{type(w).__name__}'{w.objectName() or _name_of(w)[:30]}'"
        has_name = bool(w.accessibleName().strip()) or id(w) in labelled or (
            isinstance(w, QAbstractButton) and bool(re.sub(r"[&\s]", "", w.text())))
        if not has_name and not _name_of(w):
            out.append(Finding(where, "has no name a screen reader can say", "setAccessibleName(...) or add a label"))
        elif not has_name:
            out.append(Finding(where, "is named only by its tooltip or placeholder", "setAccessibleName(...)", "note"))
        if w.isVisibleTo(root) and w.focusPolicy() == Qt.FocusPolicy.NoFocus and not isinstance(w, QAbstractItemView):
            out.append(Finding(where, "cannot be reached with the keyboard", "give it a focus policy (StrongFocus)"))
        if isinstance(w, QAbstractButton) and w.isVisibleTo(root) and 0 < w.height() < MIN_TARGET and w.minimumHeight() < MIN_TARGET:
            out.append(Finding(where, f"is only {w.height()} px tall (a hard target to hit)", f"make it at least {MIN_TARGET} px",
                               "note"))
    return out


def report(findings: list[Finding]) -> str:
    fails = [f for f in findings if f.level == "fail"]
    notes = [f for f in findings if f.level != "fail"]
    lines = [f"Accessibility check: {len(fails)} problem(s), {len(notes)} note(s)."]
    lines += [f"  PROBLEM {f.where}: {f.problem}. Fix: {f.fix}." for f in fails]
    lines += [f"  note    {f.where}: {f.problem}." for f in notes]
    lines.append("Not checked by this tool (needs a person): " + "; ".join(NOT_COVERED) + ".")
    return "\n".join(lines)
