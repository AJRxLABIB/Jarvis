"""Before/after verification for the computer agent (screen actions).

The rule is: never claim a screen action worked without checking. Every action that drives the screen - clicking,
typing, pressing keys, scrolling - takes a cheap snapshot of the screen before and after and compares them, so the
result can say "verified: the screen changed / the text appeared" or, honestly, "nothing changed - it may not have
worked". None means the check itself couldn't run (no capture), never a false "it worked".

The signals are deliberately light and local (a small grey thumbnail, the active window title, and - when already
available - the OCR text). Nothing here is saved; images stay in memory. The comparison logic is pure so it can be
tested without a real screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CHANGE_THRESHOLD = 0.6  # mean 0-255 brightness change of the small grey thumbnail


@dataclass
class Snapshot:
    thumb: Any = None        # small grey image (PIL) or None if capture failed
    foreground: str = ""     # active window title
    ocr_text: str = ""       # lowercased visible text, when it was already read (else "")


@dataclass
class Verdict:
    ok: bool | None          # True = verified an effect, False = no visible effect, None = couldn't check
    detail: str = ""

    @property
    def note(self) -> str:
        if self.ok is False:
            return " Nothing on the screen changed afterwards, so it may not have worked."
        return ""


def pixels_changed(before: Any, after: Any, threshold: float = CHANGE_THRESHOLD) -> bool | None:
    """True when the two grey thumbnails differ by at least ``threshold`` on average. None if either is missing."""
    if before is None or after is None:
        return None
    import numpy as np

    a = np.asarray(before, dtype=np.float32)
    b = np.asarray(after, dtype=np.float32)
    if a.shape != b.shape:
        return True  # a resize/relayout is itself a change
    return float(np.abs(a - b).mean()) >= threshold


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def text_appeared(after_ocr: str, wanted: str) -> bool:
    """Did (most of) ``wanted`` show up in the after-OCR? Forgiving: OCR is imperfect, so a solid prefix counts."""
    want = _norm(wanted)
    have = _norm(after_ocr)
    if not want or not have:
        return False
    if want in have:
        return True
    probe = want[:24].strip()  # a leading chunk is enough to say typing landed
    return len(probe) >= 4 and probe in have


def foreground_changed(before: Snapshot, after: Snapshot) -> bool:
    return bool(before.foreground) and bool(after.foreground) and before.foreground != after.foreground


def verify(before: Snapshot, after: Snapshot, *, expect_text: str = "", expect_foreground_change: bool = False,
           threshold: float = CHANGE_THRESHOLD) -> Verdict:
    """Combine the signals into one honest verdict. Any positive signal confirms; if none fire but we could look,
    it's a negative; if we couldn't look at all, it's None."""
    if before is None or after is None:
        return Verdict(None, "couldn't capture the screen to check")
    if expect_text and after.ocr_text and text_appeared(after.ocr_text, expect_text):
        return Verdict(True, "the typed text appeared on screen")
    if expect_foreground_change and foreground_changed(before, after):
        return Verdict(True, f"the active window changed to {after.foreground!r}")
    moved = pixels_changed(before.thumb, after.thumb, threshold)
    if moved is True:
        return Verdict(True, "the screen changed")
    if moved is False:
        # a foreground change is still an effect even if the thumbnail of the main monitor barely moved
        if foreground_changed(before, after):
            return Verdict(True, f"the active window changed to {after.foreground!r}")
        return Verdict(False, "the screen did not change")
    return Verdict(None, "couldn't compare the screen")
