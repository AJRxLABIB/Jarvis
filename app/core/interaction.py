"""Adaptive interaction: which way of talking to Jarvis fits right now - voice, keyboard, gestures or the phone - and which way Jarvis should answer.

Advice, not control: it never switches anything by itself. Deterministic rules over facts that are passed in (so it is tested without a PC):
in a meeting the microphone and speaker are the wrong channels (keyboard or phone text); in presentation / gaming mode with a full-screen app, the phone
or gestures are better than a window; in sleep / quiet mode nothing should be spoken; recent typing means keyboard; otherwise voice when a microphone
is available. Every channel comes with the reason, and an unavailable channel says what is missing. Unknown facts are treated as unknown, not as "no".
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHANNELS = ("voice", "keyboard", "gestures", "phone")


@dataclass(frozen=True)
class Context:
    mode: str = "normal"
    in_meeting: bool = False
    fullscreen: bool | None = None
    mic_available: bool | None = None
    mic_muted: bool = False
    gestures_enabled: bool = False
    camera_available: bool | None = None
    phone_connected: bool | None = None
    typing_recent: bool = False


@dataclass
class Advice:
    ranked: list[tuple[str, str]] = field(default_factory=list)       # (channel, reason), best first
    unavailable: list[tuple[str, str]] = field(default_factory=list)
    speak_replies: bool = True
    reply_note: str = ""

    def text(self) -> str:
        lines = [f"{i}. {c}: {why}" for i, (c, why) in enumerate(self.ranked, 1)]
        lines += [f"not now - {c}: {why}" for c, why in self.unavailable]
        lines.append(("Replies: spoken. " if self.speak_replies else "Replies: shown as text, not spoken. ") + self.reply_note)
        return "\n".join(lines).strip()


def advise(c: Context) -> Advice:
    score = {k: 0 for k in CHANNELS}
    why = {k: "" for k in CHANNELS}
    out: list[tuple[str, str]] = []

    def no(channel: str, reason: str) -> None:
        score[channel] = -99
        out.append((channel, reason))

    speak, note = True, ""
    quiet_modes = {"sleep", "quiet", "presentation", "meeting"}
    if c.in_meeting:
        no("voice", "you are in a meeting: the microphone and speaker would be heard")
        speak, note = False, "A meeting is in progress."
        score["keyboard"] += 3; why["keyboard"] = "silent, fine during a meeting"
        score["phone"] += 2; why["phone"] = "silent text from your phone"
    if c.mode in quiet_modes:
        speak = False
        note = (note + f" {c.mode.capitalize()} mode keeps replies silent.").strip()
        if c.mode in ("sleep", "quiet") and score["voice"] > -99:
            score["voice"] -= 1
    if c.mic_available is False:
        no("voice", "no microphone found")
    elif c.mic_muted:
        no("voice", "the microphone is muted")
    if c.gestures_enabled:
        if c.camera_available is False:
            no("gestures", "no camera available")
        else:
            score["gestures"] += 1; why["gestures"] = "hands-free and silent" + ("" if c.camera_available else " (camera state unknown)")
    else:
        no("gestures", "gesture control is switched off")
    if c.phone_connected is False:
        no("phone", "no paired phone is connected")
    elif c.phone_connected:
        score["phone"] += 1; why["phone"] = why["phone"] or "your phone is connected"
    if c.fullscreen:
        score["phone"] += 2; score["gestures"] += 1 if score["gestures"] > -99 else 0
        why["phone"] = "a full-screen app is in front; the phone does not cover it"
        if score["keyboard"] <= 0:
            score["keyboard"] -= 1
    if c.typing_recent:
        score["keyboard"] += 3; why["keyboard"] = "you are typing right now"
    if score["voice"] > -99 and not c.in_meeting:
        score["voice"] += 2
        why["voice"] = "a microphone is available" + (" (not confirmed)" if c.mic_available is None else "")
    score["keyboard"] += 1
    why["keyboard"] = why["keyboard"] or "always available"
    ranked = [(k, why[k] or "available") for k in sorted(CHANNELS, key=lambda k: -score[k]) if score[k] > -99]
    return Advice(ranked, out, speak, note)
