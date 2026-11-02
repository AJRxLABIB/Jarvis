"""Audio output routing: "put it on the speakers / headphones / Bluetooth" - which playback device to switch to, decided from the devices Windows lists.

Pure logic (no Windows calls): devices come in as ``Device`` records from an environment, the answer is a ``Decision``. The Windows side lives in
app/tools/audio_win.py and is NOT verified by the automated tests (it needs your real devices; ``audio_output`` action ``list`` shows what it sees).

Rules, in plain words:
* Only ACTIVE playback devices are considered (unplugged / disconnected ones are not listed as active by Windows).
* speakers   = the PC's own / wired speakers (never a Bluetooth device while a normal one exists).
* headphones = wired headphones or headset.
* bluetooth  = a connected Bluetooth device ("Hands-Free" call profiles are only used when nothing else exists).
* connected  = "the connected speaker": Bluetooth first, then a USB device, then a screen / HDMI device.
* a device name ("JBL", "Realtek") picks that device.
* If the wanted device is not there, play on the PC speakers (as asked) and SAY so; if even those are missing, change nothing and say why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SPEAKERS, HEADPHONES, BLUETOOTH, USB, DISPLAY, OTHER = "speakers", "headphones", "bluetooth", "usb", "display", "other"
TARGETS = ("speakers", "headphones", "bluetooth", "connected", "previous", "any")
LOUD_PERCENT = 70          # "loudly": raise the speaker volume to at least this (never to 100% by itself: sudden full volume is unkind)
SPEAKER_MIN_PERCENT = 40   # moving to speakers never leaves them near-silent

_BT_NAME = re.compile(r"bluetooth|\bbt\b|airpods|\bbuds?\b|earbuds?|jbl|bose|sony|wh-\d|wf-\d|soundcore|anker|marshall|beats|sennheiser|oneplus|galaxy buds|"
                      r"harman|ue boom|megaboom|tws", re.I)
_HANDSFREE = re.compile(r"hands[- ]?free|\bhfp\b|\bag audio\b", re.I)
# MMDevice form factors: 1 Speakers, 2 LineLevel, 3 Headphones, 4 Microphone, 5 Headset, 6 Handset, 7 UnknownDigitalPassthrough, 8 SPDIF, 9 HDMI, 10 Unknown
FORM_SPEAKERS, FORM_HEADPHONES, FORM_HEADSET, FORM_SPDIF, FORM_HDMI = 1, 3, 5, 8, 9


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    form_factor: int = 10
    enumerator: str = ""         # "BTHENUM" / "USB" / "HDAUDIO" / "MMDEVAPI" ... (Windows' device enumerator), empty if unknown
    active: bool = True
    is_default: bool = False

    @property
    def kind(self) -> str:
        enum, name = self.enumerator.upper(), self.name
        if enum.startswith("BTH") or (not enum and _BT_NAME.search(name) and "realtek" not in name.lower()):   # trust Windows' enumerator when known
            return BLUETOOTH
        if self.form_factor in (FORM_HEADPHONES, FORM_HEADSET) or re.search(r"head(phone|set)|earphone", name, re.I):
            return HEADPHONES
        if self.form_factor in (FORM_HDMI, FORM_SPDIF) or re.search(r"hdmi|display|nvidia high|intel\(r\) display|monitor|digital output", name, re.I):
            return DISPLAY
        if enum.startswith("USB"):
            return USB
        if self.form_factor == FORM_SPEAKERS or re.search(r"speaker", name, re.I):
            return SPEAKERS
        return OTHER

    @property
    def hands_free(self) -> bool:
        return bool(_HANDSFREE.search(self.name))


@dataclass
class Decision:
    device: Device | None            # what to switch to (None = change nothing)
    message: str
    fallback: bool = False           # the wanted device was missing: PC speakers were chosen instead
    already: bool = False            # it is already the default
    raise_volume_to: int | None = None
    alternatives: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def parse_target(text: str) -> tuple[str, str]:
    """Words -> (target, name hint). "bluetooth speaker" -> bluetooth; "the connected speaker" -> connected; "JBL" -> ("any", "jbl")."""
    t = _norm(text)
    if re.fullmatch(r"(?:back|previous|before|the previous(?: one)?|where it was|the last(?: one)?)", t):
        return "previous", ""
    if re.search(r"\b(bluetooth|blue tooth|bt|wireless)\b|airpods|\bbuds?\b|earbuds?", t):
        return "bluetooth", ""
    if re.search(r"\b(connected|external|other|paired)\b", t):
        return "connected", ""
    if re.search(r"head(phones?|set)|earphones?|earbuds?", t):
        return "headphones", ""
    if re.search(r"\b(speakers?|loudspeakers?|laptop|pc|computer|built in|internal)\b", t):
        return "speakers", ""
    return "any", t


_OUT_PHRASE = re.compile(
    r"[\s,]*\b(?:and\s+)?(?:(?:please\s+)?(?:put|play|boot|run|move|send|switch|route|output|set)\s+)?(?:it\s+|them\s+|that\s+|the\s+(?:sound|audio)\s+)?"
    r"(?:on|onto|to|through|via|using|in)\s+(?:the\s+|my\s+)?((?:pc\s+|laptop\s+|computer\s+|built[- ]in\s+|bluetooth\s+|connected\s+|external\s+)?"
    r"(?:loud\s*)?(?:speakers?|loudspeakers?|head(?:phones?|set)|earphones?|earbuds?|bluetooth(?:\s+(?:speaker|device|headphones?))?))\b(?:\s+please)?[\s.!]*$",
    re.I)


# Speech recognition garbles the little words ("put it on" -> "play tonic"): "<query> and <up to 3 words> speaker" still means the speaker.
_OUT_LOOSE = re.compile(r"[\s,]+and\s+(?:[a-z']+\s+){1,3}?(speakers?|loudspeakers?|head(?:phones?|set)|earphones?|earbuds?|bluetooth(?:\s+speaker)?)(?:\s+please)?[\s.!]*$", re.I)


def split_output(text: str) -> tuple[str, str]:
    """"play lofi and put it on speaker" -> ("play lofi", "speaker"); no output phrase -> (text, "")."""
    m = _OUT_PHRASE.search(text or "") or _OUT_LOOSE.search(text or "")
    if not m:
        return (text or "").strip(), ""
    return text[:m.start()].strip(" ,."), m.group(1).strip()


def _pick_speakers(devs: list[Device]) -> Device | None:
    pool = [d for d in devs if d.kind == SPEAKERS] or [d for d in devs if d.kind == OTHER and not d.hands_free and d.form_factor not in (4, 6)]
    # the PC's own speakers first: a plain "Speakers (...)" over a USB / display one
    pool.sort(key=lambda d: (d.enumerator.upper().startswith("USB"), "speaker" not in d.name.lower(), d.name))
    return pool[0] if pool else None


def _pick_bluetooth(devs: list[Device]) -> Device | None:
    pool = [d for d in devs if d.kind == BLUETOOTH]
    pool.sort(key=lambda d: (d.hands_free, not d.is_default, d.name))      # stereo profile before the "Hands-Free" call profile
    return pool[0] if pool else None


def decide(devices: list[Device], target: str, hint: str = "", *, loud: bool = False) -> Decision:
    """Choose the playback device for ``target`` (see the module doc). ``devices`` may include inactive ones; they are ignored."""
    live = [d for d in devices if d.active]
    names = [d.name for d in live]
    if not live:
        return Decision(None, "Windows lists no active sound output at all, so I changed nothing.", alternatives=names)
    current = next((d for d in live if d.is_default), None)
    want: Device | None = None
    label = target
    if hint and target != "previous":                         # a device name: "JBL", "Realtek"
        words = [w for w in _norm(hint).split() if len(w) >= 2 and w not in ("on", "the", "to", "my", "put", "it", "audio", "sound", "output", "device")]
        scored = sorted(((sum(w in _norm(d.name) for w in words), d) for d in live), key=lambda x: -x[0])
        if words and scored[0][0] > 0:
            want, label = scored[0][1], scored[0][1].name
    if want is None and target == "previous":
        want = next((d for d in live if d.id == hint), None)
        if want is None:
            return Decision(None, "I don't know which output you were using before, so I changed nothing.", alternatives=names)
    if want is None and target == "speakers":
        want = _pick_speakers(live)
    elif want is None and target == "headphones":
        want = next(iter(sorted((d for d in live if d.kind == HEADPHONES), key=lambda d: d.name)), None)
    elif want is None and target == "bluetooth":
        want = _pick_bluetooth(live)
    elif want is None and target == "connected":
        want = _pick_bluetooth(live) or next((d for d in live if d.kind == USB), None) or next((d for d in live if d.kind == DISPLAY), None)
    fallback = False
    if want is None:
        pcs = _pick_speakers(live)
        if target in ("bluetooth", "connected", "headphones", "any") and pcs is not None:
            want, fallback = pcs, True
        elif target == "speakers" and pcs is None:
            return Decision(None, "I could not find the PC speakers among the active sound outputs, so I changed nothing. I see: " + ", ".join(names) + ".",
                            alternatives=names)
        else:
            return Decision(None, f"No {label} output was found and the PC speakers are not available either, so I changed nothing.", alternatives=names)
    boost = None
    if want.kind == SPEAKERS:
        boost = LOUD_PERCENT if loud else SPEAKER_MIN_PERCENT
    if fallback:
        what = {"bluetooth": "No Bluetooth sound device is connected", "connected": "No connected speaker was found",
                "headphones": "No headphones are connected", "any": "I could not find that sound device"}[target]
        msg = f"{what}, so I'm playing on the PC speakers ({want.name})."
    else:
        msg = f"Sound now plays on {want.name}."
    already = current is not None and current.id == want.id
    if already and not fallback:
        msg = f"Sound is already playing on {want.name}."
    return Decision(want, msg, fallback=fallback, already=already, raise_volume_to=boost, alternatives=[n for n in names if n != want.name])
