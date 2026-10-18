"""Control words that must work WHILE Jarvis is busy: "stop speaking", "pause this mission", "cancel the current
task", "explain what you are doing", "show me the plan", "do not remember this conversation".

While a request runs, new requests are refused ("Still working...") - but these are not requests, they steer the one
that is running, so the window checks them first (controller.send_text -> live_control.classify). They are plain
patterns (no AI), English and Bangla, matched only on the whole sentence.
"""

from __future__ import annotations

import re

_P = r"^(?:jarvis[,!]?\s+)?(?:please\s+)?(?:can\s+you\s+)?"
_E = r"(?:\s+(?:please|now|jarvis))*[.!?।]*$"

INTENTS: tuple[tuple[str, re.Pattern], ...] = (
    ("stop_speaking", re.compile(_P + r"(?:stop|quit|stop\s+the)\s+(?:speaking|talking|reading)(?:\s+out\s+loud)?" + _E +
                                 r"|^(?:চুপ\s*(?:করো|কর)|কথা\s+বন্ধ\s+(?:করো|কর))[.!।]*$|^(?:shut\s+up|be\s+quiet|silence)" + _E,
                                 re.I)),
    ("pause_mission", re.compile(_P + r"(?:pause|hold)\s+(?:this|the|my|that)?\s*(?:mission|task|plan)" + _E +
                                 r"|^(?:মিশন|কাজ)\s+(?:থামাও|থামো|পজ\s+করো)[.!।]*$", re.I)),
    ("cancel_task", re.compile(_P + r"(?:cancel|stop|abort|kill)\s+(?:the\s+|this\s+|my\s+|that\s+)?(?:current\s+|running\s+)?"
                               r"(?:task|request|job|mission|action|everything\s+you(?:'re|\s+are)\s+doing|what\s+you(?:'re|\s+are)\s+doing)"
                               + _E + r"|^(?:বাতিল\s+করো|কাজ\s+বাতিল\s+করো)[.!।]*$", re.I)),
    ("explain_now", re.compile(_P + r"(?:explain|tell\s+me|say)\s+what\s+(?:you(?:'re|\s+are)\s+doing|you(?:'re|\s+are)\s+working\s+on)"
                               + _E + r"|" + _P + r"what\s+are\s+you\s+doing(?:\s+(?:now|right\s+now))?" + _E +
                               r"|^(?:তুমি\s+)?(?:এখন\s+)?কী\s+করছ[োে]?[?।]*$", re.I)),
    ("show_plan", re.compile(_P + r"show\s+(?:me\s+)?(?:the|your)\s+plan(?:\s+(?:before|first|before\s+continuing|before\s+you\s+continue))?"
                             + _E + r"|" + _P + r"what(?:'s|\s+is)\s+(?:the|your)\s+plan" + _E, re.I)),
    ("dont_remember", re.compile(_P + r"(?:do\s+not|don't|dont)\s+(?:remember|save|store|keep)\s+(?:this|that|anything|any\s+of\s+this)"
                                 r"(?:\s+(?:conversation|chat|talk|discussion))?" + _E +
                                 r"|^(?:এটা\s+)?মনে\s+রেখো\s+না[.!।]*$", re.I)),
)


def classify(text: str) -> str | None:
    """The control intent of ``text`` (see INTENTS), or None when it's an ordinary request."""
    cleaned = " ".join((text or "").split())
    if not cleaned or len(cleaned) > 80:
        return None
    for name, pattern in INTENTS:
        if pattern.match(cleaned):
            return name
    return None


def plan_text(mission) -> str:
    """A running mission's plan as one readable line per step, with where it is now."""
    marks = {"done": "done", "running": "NOW", "failed": "failed", "skipped": "skipped", "waiting": "waits for you",
             "approved": "approved", "undone": "undone"}
    lines = [f"Plan for '{mission.title}' ({mission.state}):"]
    for step in mission.steps:
        state = marks.get(step.state, "to do")
        lines.append(f"{step.n}. {step.title} [{state}]")
    return "\n".join(lines)
