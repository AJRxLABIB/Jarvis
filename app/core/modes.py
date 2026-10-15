"""Modes: one word changes how Jarvis behaves ("gaming mode", "meeting mode", "private mode").

What a mode changes (deterministic, never decided by the AI):

* ``speak``        which notification priorities are also spoken aloud (the rest wait in the center)
* ``suggestions``  habit / proactive suggestions on or off
* ``remember``     whether conversations are saved for "what did I say about ...", and app habits recorded
* ``capture``      whether screen capture tools may run (off = refused, whatever SCREEN_CAPTURE_ENABLED says)
* ``verbose``      short or normal answers

A mode never grants anything: it can only make Jarvis quieter or more private, and it doesn't change permissions.
The current mode survives a restart (settings store). Focus mode is separate (a timer) and works in any mode.
"""

from __future__ import annotations

from dataclasses import dataclass

KEY = "jarvis_mode"


@dataclass(frozen=True)
class Mode:
    name: str
    speak: frozenset[str]
    suggestions: bool = True
    remember: bool = True
    capture: bool = True
    verbose: bool = True
    description: str = ""


ALL = frozenset({"low", "normal", "high", "urgent"})
MODES: dict[str, Mode] = {
    "normal": Mode("normal", frozenset({"normal", "high", "urgent"}), description="everything as usual"),
    "work": Mode("work", frozenset({"high", "urgent"}), description="only important things spoken"),
    "coding": Mode("coding", frozenset({"high", "urgent"}), suggestions=False, verbose=False,
                   description="quiet, short technical answers, no suggestions"),
    "research": Mode("research", frozenset({"high", "urgent"}), suggestions=False,
                     description="no suggestions, full answers"),
    "gaming": Mode("gaming", frozenset({"urgent"}), suggestions=False, verbose=False,
                   description="only urgent things spoken, short answers"),
    "presentation": Mode("presentation", frozenset(), suggestions=False, capture=False, verbose=False,
                         description="silent: nothing spoken, no screen capture, everything waits"),
    "meeting": Mode("meeting", frozenset({"urgent"}), suggestions=False, verbose=False,
                    description="only urgent things spoken"),
    "private": Mode("private", frozenset({"high", "urgent"}), suggestions=False, remember=False, capture=False,
                    description="nothing saved from conversations, no habits recorded, no screen capture"),
    "maintenance": Mode("maintenance", ALL, suggestions=False, description="every notice spoken, no suggestions"),
}
ALIASES = {"normal mode": "normal", "default": "normal", "study": "research", "game": "gaming", "silent": "presentation",
           "present": "presentation", "privacy": "private", "incognito": "private", "dev": "coding",
           "programming": "coding", "office": "work"}


def resolve(name: str) -> Mode | None:
    key = " ".join(name.lower().replace("mode", " ").split())
    key = ALIASES.get(key, key)
    return MODES.get(key)


class ModeState:
    def __init__(self, settings_store=None) -> None:
        self.store = settings_store
        saved = settings_store.get(KEY) if settings_store is not None else None
        self.current: Mode = MODES.get(str(saved), MODES["normal"]) if saved else MODES["normal"]

    def set(self, mode: Mode) -> Mode:
        previous, self.current = self.current, mode
        if self.store is not None:
            self.store.set(KEY, mode.name)
        return previous

    def speaks(self, priority: str) -> bool:
        return priority in self.current.speak


STATE = ModeState()  # replaced with a stored one at start-up (the window does it)
