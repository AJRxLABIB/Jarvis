"""Gesture profiles: named bundles of INPUT behaviour for a use case, plus which app suggests which profile.

A profile only changes how hand input is interpreted (reach, smoothness, which gestures are active, what a palm swipe sends). It
never maps a gesture to a JARVIS tool, so switching profile can never make a gesture start doing something dangerous: tools are
bound separately as macros (app/gesture/macros.py), each scoped to a profile and each going through the normal permission path.
Pure data + functions: no camera, no Windows.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    summary: str
    settings: dict = field(default_factory=dict)         # GestureSettings fields (applied with GestureService.configure)
    apps: tuple[str, ...] = ()                           # foreground process names that suggest this profile


_BASE = {"reach": "medium", "smooth": "balanced", "zoom": True, "swipe": "off", "drag": False, "right_click": False,
         "double_click": False, "dwell_click": False, "fist_drag": False, "max_hands": 2}

PROFILES: dict[str, Profile] = {p.name: p for p in (
    Profile("general", "General desktop", "Point, pinch to click, two-finger scroll, two-hand zoom. Nothing risky is on."),
    Profile("browsing", "Web browsing", "Adds right click and a palm swipe for back / forward.",
            {"swipe": "browser", "right_click": True},
            ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe")),
    Profile("presentation", "Presentation", "Large, steady reach; a palm swipe changes slides; zoom and clicks stay quiet.",
            {"swipe": "slides", "reach": "large", "smooth": "smoother", "zoom": False, "max_hands": 1},
            ("powerpnt.exe", "soffice.exe", "simpress.exe", "keynote.exe", "acrord32.exe", "sumatrapdf.exe")),
    Profile("media", "Media control", "Small, calm movements; no zoom, no swipe keys (bind next / previous as macros).",
            {"reach": "small", "smooth": "smoother", "zoom": False, "max_hands": 1},
            ("vlc.exe", "spotify.exe", "wmplayer.exe", "mpc-hc64.exe", "mpc-hc.exe", "music.ui.exe", "potplayermini64.exe")),
    Profile("precision", "Precision pointer", "Small reach and heavy smoothing for fine work; drag and double click on.",
            {"reach": "small", "smooth": "smoother", "zoom": False, "drag": True, "double_click": True, "max_hands": 1},
            ("photoshop.exe", "figma.exe", "inkscape.exe", "gimp-2.10.exe", "blender.exe", "acad.exe", "krita.exe")),
    Profile("accessibility", "Accessibility", "Hold still to click (no pinch needed), wide reach, heavy smoothing, one hand.",
            {"dwell_click": True, "reach": "large", "smooth": "smoother", "zoom": False, "max_hands": 1}),
)}

DEFAULT = "general"


def settings_for(name: str) -> dict:
    """The full settings dict a profile applies (the base, then the profile's own changes)."""
    profile = PROFILES.get(name)
    if profile is None:
        raise ValueError("Profiles: " + ", ".join(PROFILES))
    return {**_BASE, **profile.settings}


def suggest_for(process: str, title: str = "") -> str | None:
    """The profile an app suggests, or None. A suggestion only: it applies automatically only when the user turned that on."""
    exe = (process or "").strip().lower()
    for profile in PROFILES.values():
        if exe and exe in profile.apps:
            return profile.name
    return None


def describe(name: str) -> str:
    p = PROFILES[name]
    return f"{p.label}: {p.summary}"
