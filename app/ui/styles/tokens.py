"""Design tokens: the one place the app's colours, type and shape live.

`theme.py` builds the Qt stylesheet from these and `app.core.a11y` audits their contrast, so changing a value here
changes the whole UI and is checked by the accessibility audit. Values are plain strings/numbers (no Qt) so the file can
be exported to JSON for other front ends (`python run.py --export-tokens`).
"""

from __future__ import annotations

COLOR: dict[str, str] = {
    # surfaces
    "bg": "#02050b", "panel": "#060e1a", "panel_edge": "#153a57", "field": "#050c16", "button": "#081626",
    "button_disabled_text": "#3b5068", "button_disabled_edge": "#0d2033", "progress": "#06101c",
    # text
    "text": "#d6e6f5", "text_strong": "#e8f6ff", "dim": "#5f7f9c", "chip_text": "#9fc6e4", "on_accent": "#effbff",
    # accents
    "accent": "#38bdf8", "accent_soft": "#0e3a52", "accent_strong": "#0b4a66", "accent_strong_hover": "#0e5d80",
    "bubble_user": "#0b3a56", "bubble_ai": "#0a1b2c", "bubble_user_edge": "#2a8fc4", "bubble_ai_edge": "#1d5f80",
    "gold": "#f5b642", "cyan": "#22d3ee", "dash_edge": "#0f3a55",
    # status
    "warn": "#fbbf24", "violet": "#a78bfa", "danger": "#f87171", "danger_bg": "#4c1414", "danger_text": "#fff1f2",
    "ok": "#34d399", "ok_bg": "#064e3b", "ok_text": "#ecfdf5", "ok_dark_bg": "#062a1f", "live_bg": "#04101c",
}

STATE_COLOR: dict[str, str] = {
    "IDLE": "#38bdf8", "LISTENING": "#34d399", "THINKING": "#a78bfa", "EXECUTING": "#fbbf24",
    "WAITING_FOR_PERMISSION": "#f5b642", "SPEAKING": "#60a5fa", "ERROR": "#f87171",
}

FONT: dict[str, str] = {
    "sans": "'Segoe UI', 'Inter', sans-serif",
    "mono": "'Cascadia Mono', 'Consolas', 'Courier New', monospace",
}

SPACING: dict[str, int] = {"xs": 2, "sm": 4, "md": 8, "lg": 12, "xl": 16, "xxl": 24}   # px
RADIUS: dict[str, int] = {"none": 0, "sm": 2, "md": 4, "lg": 12, "pill": 18}           # px
TARGET_MIN = 24     # smallest comfortable click target in px (WCAG 2.2 AA minimum)


def as_dict() -> dict:
    return {"color": dict(COLOR), "state_color": dict(STATE_COLOR), "font": dict(FONT), "spacing": dict(SPACING),
            "radius": dict(RADIUS), "target_min": TARGET_MIN}
