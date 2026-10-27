"""The ten states Jarvis can be in, shown the same way everywhere (rail, status area, command field, timeline).

Every state has a label, a SHAPE glyph and a colour, so colour is never the only signal. ``resolve`` turns what the
controller reports (plus a few real facts: is the AI reachable, is an emergency stop on, is a check running) into one
of them, with a fixed priority so the display never flickers between two answers.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ui.styles.tokens import COLOR


@dataclass(frozen=True)
class StateInfo:
    key: str
    label: str
    glyph: str          # a shape, so the state reads without colour
    color: str
    hint: str           # one plain sentence for tooltips and screen readers
    busy: bool = False  # Jarvis is working (show Cancel, animate unless reduced motion)

    @property
    def text(self) -> str:
        return f"{self.glyph} {self.label}"

    @property
    def accessible(self) -> str:
        return f"Status: {self.label}. {self.hint}"


STATES: dict[str, StateInfo] = {s.key: s for s in (
    StateInfo("idle", "Idle", "○", COLOR["accent"], "Ready for your next request."),
    StateInfo("listening", "Listening", "◉", COLOR["ok"], "The microphone is open and Jarvis is hearing you."),
    StateInfo("thinking", "Thinking", "◌", COLOR["violet"], "The local AI model is working out what to do.", True),
    StateInfo("executing", "Executing", "▶", COLOR["warn"], "Jarvis is carrying out an action.", True),
    StateInfo("waiting", "Needs approval", "⏸", COLOR["gold"], "Jarvis is waiting for your answer before it continues.", True),
    StateInfo("verifying", "Verifying", "◎", COLOR["cyan"], "Jarvis is checking that the action really happened.", True),
    StateInfo("completed", "Completed", "✔", COLOR["ok"], "The last request finished and was checked."),
    StateInfo("failed", "Failed", "✖", COLOR["danger"], "The last request did not work. The details are in the task panel."),
    StateInfo("offline", "Offline", "⊘", "#94a3b8", "The local AI model isn't reachable. Direct commands still work."),
    StateInfo("emergency", "Emergency stop", "■", "#ef4444", "Everything is stopped. Turn it off in Emergency controls."),
)}
ORDER = tuple(STATES)


def resolve(controller_state: str = "IDLE", *, ai_online: bool | None = True, emergency: bool = False,
            verifying: bool = False, outcome: str | None = None) -> StateInfo:
    """Priority: emergency stop > waiting for you > listening > verifying > executing > thinking > offline > outcome > idle.

    ``outcome`` is "completed" / "failed" for a short while after a request ends (the caller clears it)."""
    if emergency:
        return STATES["emergency"]
    state = (controller_state or "IDLE").upper()
    if state == "WAITING_FOR_PERMISSION":
        return STATES["waiting"]
    if state == "LISTENING":
        return STATES["listening"]
    if verifying:
        return STATES["verifying"]
    if state in ("EXECUTING", "SPEAKING"):
        return STATES["executing"]
    if state == "THINKING":
        return STATES["thinking"]
    if state == "ERROR":
        return STATES["failed"]
    if ai_online is False:
        return STATES["offline"]
    if outcome in ("completed", "failed"):
        return STATES[outcome]
    return STATES["idle"]
