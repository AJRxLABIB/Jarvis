"""Original HUD theme: near-black blue, cyan light, thin framed panels, monospaced labels."""

from __future__ import annotations

STATE_COLORS: dict[str, str] = {
    "IDLE": "#38bdf8",       # cyan
    "LISTENING": "#34d399",  # green
    "THINKING": "#a78bfa",   # violet
    "EXECUTING": "#fbbf24",  # amber
    "WAITING_FOR_PERMISSION": "#f5b642",  # gold: your answer is needed
    "SPEAKING": "#60a5fa",   # blue
    "ERROR": "#f87171",      # red
}

STATE_LABELS: dict[str, str] = {
    "IDLE": "Ready", "LISTENING": "Listening", "THINKING": "Thinking", "EXECUTING": "Working",
    "SPEAKING": "Speaking", "ERROR": "Problem", "WAITING_FOR_PERMISSION": "Needs your OK",
}

# Palette
BG = "#02050b"
PANEL = "#060e1a"
PANEL_EDGE = "#153a57"
ACCENT = "#38bdf8"
GOLD = "#f5b642"
ACCENT_SOFT = "#0e3a52"
TEXT = "#d6e6f5"
DIM = "#5f7f9c"
MONO = "'Cascadia Mono', 'Consolas', 'Courier New', monospace"

STYLESHEET = f"""
QWidget {{ color: {TEXT}; font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 10pt; }}
QMainWindow, QDialog, QMessageBox {{ background: {BG}; }}
QAbstractItemView {{ background: #050c16; }}
QCheckBox, QRadioButton {{ background: transparent; }}
QFrame#panel {{ background: {PANEL}; border: 1px solid {PANEL_EDGE}; border-radius: 4px; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-family: {MONO}; font-size: 17pt; font-weight: 700; color: #e8f6ff; letter-spacing: 7px; }}
QLabel#gold {{ font-family: {MONO}; font-size: 8pt; color: {GOLD}; letter-spacing: 3px; }}
QLabel#sectionLabel {{ font-family: {MONO}; font-size: 7.5pt; font-weight: 700; color: {ACCENT}; letter-spacing: 3px; }}
QPushButton#chip {{ background: rgba(56,189,248,0.06); border: 1px solid #153a57; border-radius: 12px;
                    padding: 5px 12px; font-size: 8pt; color: #9fc6e4; letter-spacing: 1px; }}
QPushButton#chip:hover {{ border-color: {GOLD}; color: #fff4dc; background: rgba(245,182,66,0.10); }}
QLabel#subtitle {{ font-family: {MONO}; font-size: 8pt; color: {DIM}; letter-spacing: 3px; }}
QLabel#muted {{ color: {DIM}; }}
QLabel#hudLabel {{ font-family: {MONO}; font-size: 8pt; color: {DIM}; letter-spacing: 2px; }}
QLabel#hudValue {{ font-family: {MONO}; font-size: 10pt; color: {TEXT}; }}
QLabel#state {{ font-family: {MONO}; font-size: 13pt; font-weight: 700; letter-spacing: 6px; }}
QLabel#hint {{ font-family: {MONO}; font-size: 8pt; color: {DIM}; letter-spacing: 1px; }}
QLabel#clock {{ font-family: {MONO}; font-size: 22pt; font-weight: 300; color: #e8f6ff; letter-spacing: 3px; }}
QLabel#date {{ font-family: {MONO}; font-size: 8pt; color: {DIM}; letter-spacing: 2px; }}
QLabel#pill {{ font-family: {MONO}; font-size: 8pt; font-weight: 700; letter-spacing: 2px; padding: 3px 10px;
               border-radius: 10px; }}
QLabel#badge {{ border-radius: 3px; padding: 2px 8px; font-family: {MONO}; font-size: 7.5pt; font-weight: 700;
                letter-spacing: 1px; }}
QTextBrowser {{ background: transparent; border: none; padding: 4px 8px; }}
QLineEdit {{ background: rgba(4,12,24,0.85); border: 1px solid {PANEL_EDGE}; border-radius: 18px; padding: 9px 16px;
             color: #e8f6ff; font-size: 10.5pt; selection-background-color: {ACCENT_SOFT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QPushButton {{ background: #081626; border: 1px solid {PANEL_EDGE}; border-radius: 4px; padding: 8px 14px;
               color: {TEXT}; font-family: {MONO}; font-size: 9pt; letter-spacing: 1px; }}
QPushButton:hover {{ border-color: {ACCENT}; color: #ffffff; }}
QPushButton:pressed {{ background: {ACCENT_SOFT}; }}
QPushButton:disabled {{ color: #3b5068; border-color: #0d2033; }}
QPushButton#primary {{ background: #0b4a66; border-color: {ACCENT}; color: #effbff; font-weight: 700; }}
QPushButton#primary:hover {{ background: #0e5d80; }}
QPushButton#danger {{ background: #4c1414; border-color: #f87171; color: #fff1f2; font-weight: 700; }}
QPushButton#mic[active="true"] {{ background: #064e3b; border-color: #34d399; color: #ecfdf5; }}
QPushButton#icon {{ padding: 6px 10px; font-size: 11pt; }}
QProgressBar {{ background: #06101c; border: 1px solid {PANEL_EDGE}; border-radius: 2px; height: 6px; }}
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
QComboBox, QSpinBox, QDoubleSpinBox {{ background: #050c16; border: 1px solid {PANEL_EDGE}; border-radius: 4px;
                                       padding: 5px 8px; }}
QTableWidget, QListWidget, QPlainTextEdit {{ background: #050c16; border: 1px solid {PANEL_EDGE};
                                             gridline-color: {PANEL_EDGE}; }}
QHeaderView::section {{ background: {PANEL}; border: none; border-bottom: 1px solid {PANEL_EDGE}; padding: 5px;
                        font-family: {MONO}; font-size: 8pt; color: {DIM}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {ACCENT}; padding: 4px; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {PANEL_EDGE}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QFrame#dashHeader {{ background: rgba(3,13,24,235); border: 1px solid #0f3a55; border-top: 2px solid #22d3ee; }}
QLabel#dashSub {{ font-family: {MONO}; font-size: 7.5pt; font-weight: 700; color: #22d3ee; letter-spacing: 3px; }}
QLabel#dashNav {{ font-family: {MONO}; font-size: 8pt; color: {TEXT}; letter-spacing: 1px; }}
QLabel#dashClock {{ font-family: {MONO}; font-size: 10pt; font-weight: 700; color: #e8f6ff; letter-spacing: 2px; }}
QLabel#dashOnline {{ font-family: {MONO}; font-size: 10pt; font-weight: 700; color: {GOLD}; letter-spacing: 1px; }}
QLabel#dashBig {{ font-family: {MONO}; font-size: 20pt; font-weight: 700; color: #34d399; }}
QLabel#dashMuted {{ font-family: {MONO}; font-size: 8pt; color: {DIM}; }}
QPushButton#dashAction {{ background: rgba(4,16,28,0.92); border: 1px solid #0f3a55; border-radius: 0; text-align: left;
                          padding: 8px 12px; font-family: {MONO}; font-size: 8pt; color: {TEXT}; letter-spacing: 1px; }}
QPushButton#dashAction:hover {{ border-color: #22d3ee; color: #ffffff; background: rgba(34,211,238,0.08); }}
QLineEdit#dashInput {{ border-radius: 0; background: rgba(3,13,24,0.95); border: 1px solid #0f3a55; font-family: {MONO};
                       font-size: 9.5pt; padding: 9px 14px; }}
QLineEdit#dashInput:focus {{ border-color: #22d3ee; }}
QPushButton#liveVoice {{ background: #04101c; border: 1px solid #22d3ee; border-radius: 0; color: #22d3ee;
                         font-family: {MONO}; font-weight: 700; padding: 8px 14px; letter-spacing: 1px; }}
QPushButton#liveVoice[on="true"] {{ background: #062a1f; border-color: #34d399; color: #34d399; }}
QLabel#liveBox {{ background: rgba(3,13,24,0.95); border: 1px solid #0f3a55; border-top: 2px solid {GOLD};
                  font-family: {MONO}; font-size: 7.5pt; color: {TEXT}; padding: 4px 8px; }}
"""
