"""What the Dashboard shows, as plain data built from REAL application state (no Qt, no invented activity).

Every card says what is known and, when something is not known or not connected, says so ("not known yet", "not connected") instead of
showing a number or a green light. ``tone`` (ok / warn / bad / neutral) only reinforces the words; each card always has its words.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Card:
    key: str
    title: str
    headline: str
    lines: list[str] = field(default_factory=list)
    tone: str = "neutral"


def _count(value: Any, singular: str, plural: str | None = None) -> str:
    if value is None:
        return "not known yet"
    return f"{value} {singular if value == 1 else (plural or singular + 's')}"


def build(*, state_text: str, model: str = "", resources: str = "", mic: str = "", network: str = "",
          gesture: dict[str, Any] | None = None, perf: Any = None, mission: dict[str, Any] | None = None, unfinished: int | None = None,
          pending_approval: str = "", actions: list[dict[str, Any]] | None = None, devices: list[Any] | None = None,
          emergency: list[str] | None = None, todos: int | None = None, routines: int | None = None) -> list[Card]:
    cards: list[Card] = []
    stop = emergency or []
    cards.append(Card("ready", "Jarvis", state_text or "state not known yet",
                      ([f"Mode: {perf.describe()}"] if perf is not None else []) + ([f"Emergency controls ON: {', '.join(stop)}"] if stop else []),
                      "bad" if stop else "ok"))
    cards.append(Card("model", "Local model", model or "not known yet", [resources] if resources else [], "neutral" if model else "warn"))
    connected = [d for d in (devices or []) if getattr(d, "connection", "") in ("connected", "local")]
    if devices is None:
        cards.append(Card("devices", "Devices", "not known yet", [], "warn"))
    else:
        lines = [f"{getattr(d, 'name', '?')}: {getattr(d, 'connection', 'unknown')}" for d in devices[:5]]
        cards.append(Card("devices", "Devices", _count(len(connected), "device") + " connected", lines or ["No paired devices."],
                          "ok" if connected else "warn"))
    g = gesture or {}
    gtext = ("off" if not g.get("running") else ("practice (nothing clicked)" if g.get("practice") else str(g.get("state", "on")).lower().replace("_", " ")))
    glines = [f"Profile: {g['profile']}"] if g.get("running") and g.get("profile") else []
    if g.get("fps_cap"):
        glines.append(f"Hand tracking limited to {g['fps_cap']:g} frames/s by the performance mode")
    if g.get("problem"):
        glines.append(f"Problem: {g['problem']}")
    cards.append(Card("inputs", "Microphone and gestures", mic or "microphone state not known yet", [f"Gestures: {gtext}"] + glines + ([network] if network else []),
                      "bad" if g.get("problem") else "neutral"))
    if mission:
        lines = [f"Steps {mission['progress']}"] + ([f"Uncertain: {u}" for u in mission.get("uncertain", [])[:2]])
        cards.append(Card("mission", "Current mission", mission["title"], lines, "warn" if mission.get("uncertain") else "ok"))
    else:
        cards.append(Card("mission", "Current mission", "none running", [_count(unfinished, "unfinished task") if unfinished else "Nothing is waiting."]))
    cards.append(Card("approvals", "Needs your approval", pending_approval or "nothing waiting", [], "warn" if pending_approval else "ok"))
    recent = [a for a in (actions or []) if a.get("state") in ("succeeded", "failed", "uncertain", "rejected")][:5]
    lines = []
    for a in recent:
        mark = {"succeeded": "done", "failed": "FAILED", "uncertain": "UNCERTAIN - check it", "rejected": "not done"}[a["state"]]
        lines.append(f"{a['tool']}: {mark}")
    cards.append(Card("actions", "Recent actions", f"{len(recent)} since Jarvis started" if recent else "none yet", lines,
                      "bad" if any(a["state"] in ("failed", "uncertain") for a in recent) else "neutral"))
    cards.append(Card("life", "Today", _count(todos, "open to-do"), [f"Routines on: {routines if routines is not None else 'not known yet'}"]))
    if resources:
        cards[1].lines = [resources]
    return cards


def text(cards: list[Card]) -> str:
    """The same cards as plain text (screen readers, the old summary box, tests)."""
    out = []
    for c in cards:
        out.append(f"{c.title}: {c.headline}")
        out += [f"    {line}" for line in c.lines]
    return "\n".join(out)
