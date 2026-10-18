"""Original HUD theme: near-black blue, cyan light, thin framed panels, monospaced labels.

Every colour, font and size comes from `tokens.py`; this module only turns them into a Qt stylesheet.
"""

from __future__ import annotations

from app.ui.styles.tokens import COLOR as C
from app.ui.styles.tokens import FONT, STATE_COLOR

STATE_COLORS: dict[str, str] = dict(STATE_COLOR)

STATE_LABELS: dict[str, str] = {
    "IDLE": "Ready", "LISTENING": "Listening", "THINKING": "Thinking", "EXECUTING": "Working",
    "SPEAKING": "Speaking", "ERROR": "Problem", "WAITING_FOR_PERMISSION": "Needs your OK",
}

# Palette (kept as module names: other widgets import these)
BG = C["bg"]
PANEL = C["panel"]
PANEL_EDGE = C["panel_edge"]
ACCENT = C["accent"]
GOLD = C["gold"]
ACCENT_SOFT = C["accent_soft"]
TEXT = C["text"]
DIM = C["dim"]
MONO = FONT["mono"]
SANS = FONT["sans"]

STYLESHEET = f"""
QWidget {{ color: {TEXT}; font-family: {SANS}; font-size: 10pt; }}
QMainWindow, QDialog, QMessageBox {{ background: {BG}; }}
QAbstractItemView {{ background: {C['field']}; }}
QCheckBox, QRadioButton {{ background: transparent; }}
QFrame#panel {{ background: {PANEL}; border: 1px solid {PANEL_EDGE}; border-radius: 4px; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-family: {MONO}; font-size: 17pt; font-weight: 700; color: {C['text_strong']}; letter-spacing: 7px; }}
QLabel#gold {{ font-family: {MONO}; font-size: 8pt; color: {GOLD}; letter-spacing: 3px; }}
QLabel#sectionLabel {{ font-family: {MONO}; font-size: 7.5pt; font-weight: 700; color: {ACCENT}; letter-spacing: 3px; }}
QPushButton#chip {{ background: rgba(56,189,248,0.06); border: 1px solid {C['panel_edge']}; border-radius: 12px;
                    padding: 5px 12px; font-size: 8pt; color: {C['chip_text']}; letter-spacing: 1px; }}
QPushButton#chip:hover {{ border-color: {GOLD}; color: #fff4dc; background: rgba(245,182,66,0.10); }}
QLabel#subtitle {{ font-family: {MONO}; font-size: 8pt; color: {DIM}; letter-spacing: 3px; }}
QLabel#muted {{ color: {DIM}; }}
QLabel#hudLabel {{ font-family: {MONO}; font-size: 8pt; color: {DIM}; letter-spacing: 2px; }}
QLabel#hudValue {{ font-family: {MONO}; font-size: 10pt; color: {TEXT}; }}
QLabel#state {{ font-family: {MONO}; font-size: 13pt; font-weight: 700; letter-spacing: 6px; }}
QLabel#hint {{ font-family: {MONO}; font-size: 8pt; color: {DIM}; letter-spacing: 1px; }}
QLabel#clock {{ font-family: {MONO}; font-size: 22pt; font-weight: 300; color: {C['text_strong']}; letter-spacing: 3px; }}
QLabel#date {{ font-family: {MONO}; font-size: 8pt; color: {DIM}; letter-spacing: 2px; }}
QLabel#pill {{ font-family: {MONO}; font-size: 8pt; font-weight: 700; letter-spacing: 2px; padding: 3px 10px;
               border-radius: 10px; }}
QLabel#badge {{ border-radius: 3px; padding: 2px 8px; font-family: {MONO}; font-size: 7.5pt; font-weight: 700;
                letter-spacing: 1px; }}
QTextBrowser {{ background: transparent; border: none; padding: 4px 8px; }}
QLineEdit {{ background: rgba(4,12,24,0.85); border: 1px solid {PANEL_EDGE}; border-radius: 18px; padding: 9px 16px;
             color: {C['text_strong']}; font-size: 10.5pt; selection-background-color: {ACCENT_SOFT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QPushButton {{ background: {C['button']}; border: 1px solid {PANEL_EDGE}; border-radius: 4px; padding: 8px 14px;
               color: {TEXT}; font-family: {MONO}; font-size: 9pt; letter-spacing: 1px; }}
QPushButton:hover {{ border-color: {ACCENT}; color: #ffffff; }}
QPushButton:pressed {{ background: {ACCENT_SOFT}; }}
QPushButton:disabled {{ color: {C['button_disabled_text']}; border-color: {C['button_disabled_edge']}; }}
QPushButton#primary {{ background: {C['accent_strong']}; border-color: {ACCENT}; color: {C['on_accent']}; font-weight: 700; }}
QPushButton#primary:hover {{ background: {C['accent_strong_hover']}; }}
QPushButton#danger {{ background: {C['danger_bg']}; border-color: {C['danger']}; color: {C['danger_text']}; font-weight: 700; }}
QPushButton#mic[active="true"] {{ background: {C['ok_bg']}; border-color: {C['ok']}; color: {C['ok_text']}; }}
QPushButton#icon {{ padding: 6px 10px; font-size: 11pt; }}
QProgressBar {{ background: {C['progress']}; border: 1px solid {PANEL_EDGE}; border-radius: 2px; height: 6px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 1px; }}
QStatusBar {{ background: transparent; color: {DIM}; font-family: {MONO}; font-size: 7.5pt; letter-spacing: 2px; }}
QMenu {{ background: {PANEL}; border: 1px solid {PANEL_EDGE}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; }}
QMenu::separator {{ height: 1px; background: {PANEL_EDGE}; margin: 4px 8px; }}
QTabWidget::pane {{ border: 1px solid {PANEL_EDGE}; border-radius: 4px; }}
QTabBar::tab {{ background: {PANEL}; border: 1px solid {PANEL_EDGE}; padding: 6px 12px; font-family: {MONO};
                font-size: 8.5pt; }}
QTabBar::tab:selected {{ background: {ACCENT_SOFT}; color: #ffffff; }}
QComboBox, QSpinBox, QDoubleSpinBox {{ background: {C['field']}; border: 1px solid {PANEL_EDGE}; border-radius: 4px;
                                       padding: 5px 8px; }}
QTableWidget, QListWidget, QPlainTextEdit {{ background: {C['field']}; border: 1px solid {PANEL_EDGE};
                                             gridline-color: {PANEL_EDGE}; }}
QHeaderView::section {{ background: {PANEL}; border: none; border-bottom: 1px solid {PANEL_EDGE}; padding: 5px;
                        font-family: {MONO}; font-size: 8pt; color: {DIM}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {ACCENT}; padding: 4px; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {PANEL_EDGE}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QFrame#dashHeader {{ background: rgba(3,13,24,235); border: 1px solid {C['dash_edge']}; border-top: 2px solid {C['cyan']}; }}
QPushButton#navItem {{ background: transparent; border: 1px solid transparent; border-radius: 3px; padding: 4px 12px;
    font-family: {MONO}; font-size: 8pt; font-weight: 700; letter-spacing: 1px; min-height: 18px; }}
QPushButton#navItem:hover {{ border: 1px solid {C['dash_edge']}; background: rgba(34, 211, 238, 0.06); }}
QLabel#engineLine {{ font-family: {MONO}; font-size: 7pt; color: {C['dim']}; letter-spacing: 1px; }}
QPushButton#micSmall {{ min-width: 30px; max-width: 34px; padding: 4px; }}
QLabel#dashSub {{ font-family: {MONO}; font-size: 7.5pt; font-weight: 700; color: {C['cyan']}; letter-spacing: 3px; }}
QLabel#dashNav {{ font-family: {MONO}; font-size: 8pt; color: {TEXT}; letter-spacing: 1px; }}
QLabel#dashClock {{ font-family: {MONO}; font-size: 10pt; font-weight: 700; color: {C['text_strong']}; letter-spacing: 2px; }}
QLabel#dashOnline {{ font-family: {MONO}; font-size: 10pt; font-weight: 700; color: {GOLD}; letter-spacing: 1px; }}
QLabel#dashBig {{ font-family: {MONO}; font-size: 20pt; font-weight: 700; color: {C['ok']}; }}
QLabel#dashMuted {{ font-family: {MONO}; font-size: 8pt; color: {DIM}; }}
QPushButton#dashAction {{ background: rgba(4,16,28,0.92); border: 1px solid {C['dash_edge']}; border-radius: 0; text-align: left;
                          padding: 8px 12px; font-family: {MONO}; font-size: 8pt; color: {TEXT}; letter-spacing: 1px; }}
QPushButton#dashAction:hover {{ border-color: {C['cyan']}; color: #ffffff; background: rgba(34,211,238,0.08); }}
QLineEdit#dashInput {{ border-radius: 0; background: rgba(3,13,24,0.95); border: 1px solid {C['dash_edge']}; font-family: {MONO};
                       font-size: 9.5pt; padding: 9px 14px; }}
QLineEdit#dashInput:focus {{ border-color: {C['cyan']}; }}
QPushButton#liveVoice {{ background: {C['live_bg']}; border: 1px solid {C['cyan']}; border-radius: 0; color: {C['cyan']};
                         font-family: {MONO}; font-weight: 700; padding: 8px 14px; letter-spacing: 1px; }}
QPushButton#liveVoice[on="true"] {{ background: {C['ok_dark_bg']}; border-color: {C['ok']}; color: {C['ok']}; }}
QLabel#liveBox {{ background: rgba(3,13,24,0.95); border: 1px solid {C['dash_edge']}; border-top: 2px solid {GOLD};
                  font-family: {MONO}; font-size: 7.5pt; color: {TEXT}; padding: 4px 8px; }}
"""
