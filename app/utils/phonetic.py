"""Sound-alike matching for names that speech recognition mishears.

"close bone link" / "close Bon Linked" -> "Phone Link", "open spot if I" -> "Spotify".
Each word becomes a consonant-class code (Soundex-style: b/f/p/v sound alike, so do
c/g/j/k/q/s/x/z, d/t, m/n), then codes and spellings are compared. Used only as the
last step, after exact and word-by-word matching found nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher

_WORD = re.compile(r"[a-z0-9]+")
_CLASSES = {
    **dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"), **dict.fromkeys("dt", "3"),
    "l": "4", **dict.fromkeys("mn", "5"), "r": "6",
}
_DIGRAPHS = (("ph", "f"), ("ck", "k"), ("sh", "s"), ("ch", "s"), ("th", "t"), ("wh", "w"), ("gh", ""),
             ("kn", "n"), ("wr", "r"), ("qu", "k"))
_NOISE = {"the", "app", "application", "program", "my", "a", "an", "please"}


def _word_code(word: str) -> str:
    for a, b in _DIGRAPHS:
        word = word.replace(a, b)
    if word.endswith("ed") and len(word) > 4:  # "linked" ~ "link"
        word = word[:-2]
    code, last = [], ""
    for ch in word:
        if ch.isdigit():
            code.append(ch)
            last = ""
            continue
        c = _CLASSES.get(ch, "")
        if c and c != last:
            code.append(c)
        last = c
    if not code and word:  # all vowels ("I", "a")
        code.append("0")
    return "".join(code)


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _NOISE]


def phonetic_key(text: str) -> str:
    return " ".join(_word_code(w) for w in words(text))


def similarity(heard: str, name: str) -> float:
    """0..1: how much ``heard`` sounds like ``name`` (sound counts more than spelling)."""
    a, b = phonetic_key(heard), phonetic_key(name)
    if not a or not b:
        return 0.0
    sound = SequenceMatcher(None, a.replace(" ", ""), b.replace(" ", "")).ratio()
    spelled = SequenceMatcher(None, "".join(words(heard)), "".join(words(name))).ratio()
    # the same number of words is a strong hint ("bone link" / "phone link")
    shape = 1.0 if len(a.split()) == len(b.split()) else 0.85
    return (0.7 * sound + 0.3 * spelled) * shape


def best_sounding(heard: str, names: Iterable[str], *, threshold: float = 0.72) -> str | None:
    """The name that sounds most like ``heard``, if it is clearly close enough and clearly the best."""
    scored = sorted(((similarity(heard, n), n) for n in dict.fromkeys(names) if n), reverse=True)
    if not scored or scored[0][0] < threshold:
        return None
    if len(scored) > 1 and scored[1][0] > scored[0][0] - 0.03 and phonetic_key(scored[1][1]) != phonetic_key(scored[0][1]):
        return None  # two names sound equally close: don't guess
    return scored[0][1]
