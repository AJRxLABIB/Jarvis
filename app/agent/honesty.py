"""Keeping the AI's words and Jarvis's actions in step.

Small local models sometimes SAY they did something ("Opening WhatsApp now", "WhatsApp is open") without calling
a tool - nothing happened. And after Jarvis offers "I can open WhatsApp for you", a "do it" / "yes" goes back to the
model, which may again only talk. This module finds both situations from the text alone (plain patterns, no AI):

* ``offered_action(reply)``  - the action Jarvis offered in its last reply ("open WhatsApp"), for a following yes;
* ``is_affirmation(text)``   - "do it", "yes", "go ahead", "I'm ready", "হ্যাঁ", "করো" ...;
* ``claimed_action(reply)``  - the action a reply claims to be doing / to have done ("open WhatsApp");
* ``claims_done(reply)``     - any "done / sent / opened / started" claim at all.

The conversation then either really does the action (through the normal executor and its checks) or adds an honest
correction - it never leaves a claimed action standing that didn't run.
"""

from __future__ import annotations

import re

_VERBS = r"(?:open|launch|start|play|show|search\s+for|close)"
_OBJECT = r"(?P<obj>[A-Za-z0-9][\w .&+'-]{0,40}?)"
_END = r"(?=\s*(?:[.,!?;:—-]|for\s+you|now|right\s+now|and\b|so\b|if\b|$))"

_OFFER = re.compile(r"(?:i\s+can|i\s+could|i'?ll|let\s+me|want\s+me\s+to|shall\s+i|should\s+i|"
                    r"can\s+help\s+you|help\s+you)\s+(?:help\s+you\s+)?(?P<verb>" + _VERBS + r")\s+(?:the\s+)?"
                    + _OBJECT + _END, re.I)
_CLAIM = re.compile(r"(?:^|[.!?]\s+|—\s*|\bi(?:'ve|\s+have)\s+)(?P<verb>opening|opened|launching|launched|"
                    r"starting|started|playing|closing|closed)\s+(?:the\s+|up\s+)?" + _OBJECT + _END, re.I)
_IS_OPEN = re.compile(r"\b" + _OBJECT.replace("obj", "obj2") + r"\s+is\s+(?:now\s+)?(?:open|opened|running|launched)\b", re.I)
_DONE = re.compile(r"\bi(?:'ve|\s+have)\s+(?:just\s+)?(?:opened|sent|started|launched|closed|set|turned\s+(?:on|off)|"
                   r"created|deleted|moved|saved|called|messaged|texted|played|muted|locked)\b", re.I)
_AFFIRM = re.compile(r"^(?:jarvis[,!]?\s+)?(?:yes|yeah|yep|yup|sure|ok(?:ay)?|okey|please|go\s+ahead|do\s+it|do\s+that|"
                     r"go\s+for\s+it|please\s+do|yes\s+please|sure\s+thing|i\s*(?:'m|\s+am)\s+ready|ready|proceed|"
                     r"continue|carry\s+on|হ্যাঁ|হ্যা|জি|ঠিক\s+আছে|করো|কর|খোলো)(?:\s+(?:it|that|please|now|jarvis|sir))*"
                     r"[.!?।]*$", re.I)
_NOT_THINGS = {"it", "that", "this", "them", "you", "a", "the", "up", "now", "something", "anything", "more", "here"}

_VERB_OF = {"opening": "open", "opened": "open", "launching": "open", "launched": "open", "starting": "open",
            "started": "open", "playing": "play", "closing": "close", "closed": "close", "launch": "open",
            "start": "open", "show": "open"}


def _clean(obj: str | None) -> str | None:
    if not obj:
        return None
    obj = re.sub(r"\s+(?:app|application|for\s+you|now|on\s+your\s+(?:pc|computer|screen))$", "", obj.strip(" .,'"), flags=re.I)
    return None if not obj or obj.lower() in _NOT_THINGS or len(obj) > 40 else obj


def offered_action(reply: str) -> str | None:
    """'I can help you open WhatsApp' -> 'open WhatsApp'."""
    for match in _OFFER.finditer(reply or ""):
        obj = _clean(match.group("obj"))
        if obj:
            verb = match.group("verb").lower()
            return f"{_VERB_OF.get(verb, verb)} {obj}"
    return None


def is_affirmation(text: str) -> bool:
    return bool(_AFFIRM.match(" ".join((text or "").split())))


def claimed_action(reply: str) -> str | None:
    """'Opening WhatsApp now.' / 'WhatsApp is open.' -> 'open WhatsApp'."""
    match = _CLAIM.search(reply or "")
    if match:
        obj = _clean(match.group("obj"))
        if obj:
            return f"{_VERB_OF.get(match.group('verb').lower(), 'open')} {obj}"
    match = _IS_OPEN.search(reply or "")
    if match:
        obj = _clean(match.group("obj2"))
        if obj and obj.lower() not in ("everything", "which", "what"):
            return f"open {obj.split()[-1] if len(obj.split()) > 3 else obj}"
    return None


def claims_done(reply: str) -> bool:
    return bool(_DONE.search(reply or ""))


CORRECTION = ("\n\n(Correction: no action actually ran just now - I only said it. Say \"{cmd}\" and I'll do it.)")
CORRECTION_GENERIC = ("\n\n(Correction: no action actually ran just now - that was only my wording, nothing was "
                      "done on your PC. Tell me exactly what to do and I'll do it.)")
