"""Per-application knowledge for desktop control: how each common program labels things, its shortcuts, and how well it exposes controls.

Windows UI Automation (UIA) is the preferred way to press a control by name, but programs differ: classic Win32 apps expose everything, Office uses
its own labels, and Electron / browser apps (VS Code, Teams, Discord, Slack, Chrome, Edge) expose little until accessibility is switched on. A profile is
plain DATA + pure lookups - it never presses anything itself. ``press_control`` uses it in this order: the name you said -> this app's other labels for
it -> a keyboard shortcut offered as text (never pressed silently) -> a note on why the app hides its controls. Unknown apps get no profile and behave as
before. Labels / shortcuts are standard defaults; users who changed them in the app can still use the exact on-screen name.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    name: str
    processes: tuple[str, ...]
    uia: str = "good"                                   # good | partial | poor
    labels: dict[str, tuple[str, ...]] = field(default_factory=dict)       # what you say -> other labels this app uses
    shortcuts: dict[str, str] = field(default_factory=dict)                # what you say -> keys
    note: str = ""
    risky_dialogs: tuple[str, ...] = ()                 # dialog texts where a wrong press loses work: never auto-confirmed, always ask


_SAVE_DIALOG = ("do you want to save", "want to save changes", "save changes")
PROFILES: tuple[Profile, ...] = (
    Profile("Notepad", ("notepad",), "good", {"save": ("Save", "Save As"), "open": ("Open",), "new": ("New tab", "New window")},
            {"save": "Ctrl+S", "open": "Ctrl+O", "new": "Ctrl+N", "find": "Ctrl+F"}, risky_dialogs=_SAVE_DIALOG),
    Profile("File Explorer", ("explorer",), "good", {"new folder": ("New folder", "New", "Folder"), "rename": ("Rename",), "delete": ("Delete",), "back": ("Back",)},
            {"new folder": "Ctrl+Shift+N", "rename": "F2", "delete": "Delete", "address": "Ctrl+L", "back": "Alt+Left"},
            "Delete moves to the Recycle Bin; confirm dialogs are never auto-answered."),
    Profile("Word", ("winword",), "partial", {"save": ("Save", "Save As"), "bold": ("Bold",), "italic": ("Italic",), "print": ("Print",)},
            {"save": "Ctrl+S", "bold": "Ctrl+B", "italic": "Ctrl+I", "undo": "Ctrl+Z", "print": "Ctrl+P", "find": "Ctrl+F"},
            "Ribbon buttons change names with the window width; the shortcut is the reliable route.", _SAVE_DIALOG),
    Profile("Excel", ("excel",), "partial", {"save": ("Save", "Save As"), "filter": ("Filter", "Sort & Filter"), "sum": ("AutoSum", "Sum")},
            {"save": "Ctrl+S", "undo": "Ctrl+Z", "find": "Ctrl+F", "filter": "Ctrl+Shift+L", "sum": "Alt+="},
            "Cells are read through the sheet, not as buttons.", _SAVE_DIALOG),
    Profile("PowerPoint", ("powerpnt",), "partial", {"save": ("Save", "Save As"), "present": ("From Beginning", "Slide Show")},
            {"save": "Ctrl+S", "present": "F5", "new slide": "Ctrl+M"}, risky_dialogs=_SAVE_DIALOG),
    Profile("Outlook", ("outlook", "olk"), "partial", {"send": ("Send",), "reply": ("Reply",), "new mail": ("New Email", "New mail")},
            {"send": "Alt+S", "reply": "Ctrl+R", "new mail": "Ctrl+N"}, "Sending is never done without your approval."),
    Profile("Chrome / Edge", ("chrome", "msedge", "brave", "vivaldi"), "poor", {"new tab": ("New Tab", "New tab"), "close tab": ("Close",)},
            {"new tab": "Ctrl+T", "close tab": "Ctrl+W", "reload": "Ctrl+R", "address": "Ctrl+L", "find": "Ctrl+F", "back": "Alt+Left"},
            "Web page content is not exposed as controls until the browser's accessibility is on; use the browser tools for page content."),
    Profile("VS Code", ("code",), "poor", {"save": ("Save",), "run": ("Run", "Start Debugging")},
            {"save": "Ctrl+S", "palette": "Ctrl+Shift+P", "open file": "Ctrl+P", "terminal": "Ctrl+`", "find": "Ctrl+F", "run": "F5"},
            "VS Code is an Electron app: most controls appear only when a screen reader / accessibility mode is on (Settings > accessibility support). "
            "Prefer the 'vscode' tool and the command palette."),
    Profile("Teams / Slack / Discord", ("ms-teams", "teams", "slack", "discord"), "poor", {"send": ("Send",), "mute": ("Mute", "Unmute")},
            {"mute": "Ctrl+Shift+M", "search": "Ctrl+K", "send": "Enter"}, "Electron apps expose few controls; sending messages needs your approval."),
    Profile("Settings", ("systemsettings",), "good", {"apply": ("Apply",), "ok": ("OK",)}, {"search": "Ctrl+F"}),
)
_BY_PROCESS = {p: prof for prof in PROFILES for p in prof.processes}


def profile_for(process: str) -> Profile | None:
    return _BY_PROCESS.get((process or "").lower().removesuffix(".exe"))


def _key(text: str) -> str:
    return " ".join(text.lower().split())


def alternatives(process: str, wanted: str) -> list[str]:
    """Other labels this app uses for what you asked for (never including the wanted one itself)."""
    prof = profile_for(process)
    if prof is None:
        return []
    out = prof.labels.get(_key(wanted), ())
    return [x for x in out if _key(x) != _key(wanted)]


def shortcut(process: str, wanted: str) -> str:
    prof = profile_for(process)
    return prof.shortcuts.get(_key(wanted), "") if prof else ""


def advice(process: str, wanted: str) -> str:
    """Text to add when a control was not found: the shortcut (offered, not pressed) and why the app hides controls."""
    prof = profile_for(process)
    if prof is None:
        return ""
    parts = []
    key = shortcut(process, wanted)
    if key:
        parts.append(f"In {prof.name} the shortcut for '{wanted}' is {key} (say 'press {key}' if you want me to use it).")
    if prof.uia != "good" and prof.note:
        parts.append(prof.note)
    return " ".join(parts)


def risky_dialog(process: str, dialog_text: str) -> bool:
    """True when this app's dialog is one where a wrong automatic press can lose work (always ask)."""
    prof = profile_for(process)
    text = dialog_text.lower()
    return bool(prof and any(t in text for t in prof.risky_dialogs))
