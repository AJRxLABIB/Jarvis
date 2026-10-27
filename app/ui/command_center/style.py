"""Palettes (dark and light), text scale and the stylesheet of the command-center view.

Surfaces are restrained; the accent is used for focus and the primary action only. Every text / surface pair is checked
for contrast (WCAG AA 4.5:1) by tests/test_command_center.py through app.core.a11y.contrast. The window's global theme
is untouched: this stylesheet is set on the view's root widget only, so the 3D HUD keeps its look.
"""

from __future__ import annotations

from app.ui.styles.tokens import COLOR

PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0b1220", "rail": "#0e1627", "surface": "#111c30", "raised": "#16233b", "edge": "#2a3b57",
        "text": "#e6eef8", "dim": "#9fb3cc", "accent": "#5cc8ff", "on_accent": "#04121f", "focus": "#ffd166",
        "ok": "#4ade80", "warn": COLOR["warn"], "bad": "#ff8a8a", "bubble_user": "#17406a", "bubble_ai": "#142238",
    },
    "light": {
        "bg": "#f4f7fb", "rail": "#e8eef6", "surface": "#ffffff", "raised": "#eef3fa", "edge": "#b6c4d8",
        "text": "#0f1b2d", "dim": "#41546d", "accent": "#0b5cad", "on_accent": "#ffffff", "focus": "#8a4b00",
        "ok": "#116b3a", "warn": "#8a5a00", "bad": "#a31515", "bubble_user": "#d6e8fb", "bubble_ai": "#e7edf6",
    },
}
# (foreground key, background key) pairs that must reach 4.5:1 in both palettes
CONTRAST_PAIRS = (("text", "bg"), ("text", "surface"), ("text", "rail"), ("text", "raised"), ("dim", "bg"),
                  ("dim", "surface"), ("dim", "rail"), ("accent", "surface"), ("accent", "bg"),
                  ("on_accent", "accent"), ("ok", "surface"), ("warn", "surface"), ("bad", "surface"),
                  ("text", "bubble_user"), ("text", "bubble_ai"), ("focus", "surface"), ("focus", "rail"))


def palette(name: str) -> dict[str, str]:
    return PALETTES.get(name, PALETTES["dark"])


def scaled(point_size: float, scale: float) -> float:
    return round(point_size * max(0.8, min(1.6, scale)), 1)


def stylesheet(name: str = "dark", text_scale: float = 1.0) -> str:
    p = palette(name)
    base, small, big = scaled(10, text_scale), scaled(9, text_scale), scaled(13, text_scale)
    return f"""
#ccRoot {{ background:{p['bg']}; color:{p['text']}; font-family:'Segoe UI','Inter',sans-serif; font-size:{base}pt; }}
#ccRoot QLabel {{ color:{p['text']}; background:transparent; }}
#ccRoot QLabel#muted {{ color:{p['dim']}; font-size:{small}pt; }}
#ccRoot QLabel#h1 {{ font-size:{big}pt; font-weight:600; }}
#ccRoot QFrame#rail {{ background:{p['rail']}; border-right:1px solid {p['edge']}; }}
#ccRoot QFrame#card, #ccRoot QFrame#contextPanel {{ background:{p['surface']}; border:1px solid {p['edge']}; border-radius:8px; }}
#ccRoot QFrame#statusBar {{ background:{p['rail']}; border-top:1px solid {p['edge']}; }}
#ccRoot QFrame#commandBar {{ background:{p['surface']}; border:1px solid {p['edge']}; border-radius:10px; }}
#ccRoot QPushButton {{ background:{p['raised']}; color:{p['text']}; border:1px solid {p['edge']}; border-radius:6px;
    padding:6px 12px; min-height:24px; }}
#ccRoot QPushButton:hover {{ border-color:{p['accent']}; }}
#ccRoot QPushButton:disabled {{ color:{p['dim']}; border-style:dashed; }}
#ccRoot QPushButton#primary {{ background:{p['accent']}; color:{p['on_accent']}; border-color:{p['accent']}; font-weight:600; }}
#ccRoot QPushButton#railItem {{ text-align:left; border:none; border-radius:6px; padding:8px 12px; background:transparent; }}
#ccRoot QPushButton#railItem:checked {{ background:{p['raised']}; border-left:3px solid {p['accent']}; font-weight:600; }}
#ccRoot QPushButton#danger {{ background:{p['bad']}; color:{p['on_accent'] if name == 'light' else '#2a0606'}; font-weight:700; border-color:{p['bad']}; }}
#ccRoot QPushButton:focus, #ccRoot QLineEdit:focus, #ccRoot QListWidget:focus, #ccRoot QTextBrowser:focus,
#ccRoot QPlainTextEdit:focus, #ccRoot QTabBar::tab:focus, #ccRoot QCheckBox:focus {{ outline:none; border:2px solid {p['focus']}; }}
#ccRoot QLineEdit {{ background:{p['bg']}; color:{p['text']}; border:1px solid {p['edge']}; border-radius:6px; padding:8px; }}
#ccRoot QTextBrowser, #ccRoot QPlainTextEdit, #ccRoot QListWidget {{ background:{p['surface']}; color:{p['text']};
    border:1px solid {p['edge']}; border-radius:8px; }}
#ccRoot QTabWidget::pane {{ background:{p['surface']}; border:1px solid {p['edge']}; border-radius:6px; top:-1px; }}
#ccRoot QTabWidget > QWidget > QWidget {{ background:{p['surface']}; }}
#ccRoot QListWidget::item {{ padding:4px 6px; }}
#ccRoot QListWidget::item:selected {{ background:{p['accent']}; color:{p['on_accent']}; }}
#ccRoot QComboBox {{ background:{p['raised']}; color:{p['text']}; border:1px solid {p['edge']}; border-radius:6px; padding:4px 8px; }}
#ccRoot QComboBox QAbstractItemView {{ background:{p['surface']}; color:{p['text']}; selection-background-color:{p['accent']}; selection-color:{p['on_accent']}; }}
#ccRoot QCheckBox {{ color:{p['text']}; spacing:8px; }}
#ccRoot QSplitter::handle {{ background:{p['edge']}; width:2px; }}
#ccRoot QTabBar::tab {{ background:{p['rail']}; color:{p['dim']}; padding:6px 12px; border:1px solid {p['edge']}; }}
#ccRoot QTabBar::tab:selected {{ background:{p['surface']}; color:{p['text']}; border-bottom:2px solid {p['accent']}; }}
#ccRoot QProgressBar {{ background:{p['raised']}; border:1px solid {p['edge']}; border-radius:4px; text-align:center; height:10px; }}
#ccRoot QProgressBar::chunk {{ background:{p['accent']}; }}
#ccRoot QToolTip {{ background:{p['raised']}; color:{p['text']}; border:1px solid {p['edge']}; }}
"""
