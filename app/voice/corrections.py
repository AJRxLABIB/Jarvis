"""Learning from your corrections: "no, I said Phone Link" fixes what was misheard, now and next time.

Jarvis heard "close Phone Lane"; you say "no, I said Phone Link" (or "না, আমি বলেছি ফোন লিংক").
The part of the last request that sounds most like your correction is replaced ("phone lane" ->
"Phone Link"), the corrected request runs, and the pair is saved (data\\corrections.json). Every
later transcript gets the same fix, and the corrected words are added to the recognizer's hints.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.utils.phonetic import similarity

FILE = "corrections.json"
MAX_CORRECTIONS = 300
_CORRECTION = re.compile(
    r"^(?:no,? i said|no,? i meant|not that|that's wrong,?|i meant|i mean|i said|nope|wrong|no|না না|না|ভুল)(?![\wঀ-৿])[\s,।.!]*"
    r"(?:(?:i\s+)?(?:said|meant|mean)\s+|আমি\s+(?:বলেছি|বললাম|বলছি)\s+|বলেছি\s+)?(?P<fix>.+?)[\s.!।]*$", re.I)
_WORD = re.compile(r"[\wঀ-৿']+")
_NOT_FIXES = {"no", "nothing", "never mind", "না", "thanks", "thank you", "thank", "problem", "worries", "need",
              "stop", "wait", "it's fine", "its fine", "fine", "okay", "ok", "sir", "that's all", "ধন্যবাদ", "থাক"}


def parse_correction(text: str) -> str | None:
    """ "no, I said Phone Link" -> "Phone Link"; None if the text isn't a correction."""
    text = " ".join((text or "").split())
    if not re.match(r"^(no\b|nope\b|not that\b|wrong\b|i mean\b|i meant\b|i said\b|that's wrong\b|না\b|না|ভুল)", text, re.I):
        return None
    match = _CORRECTION.match(text)
    if not match:
        return None
    fix = match.group("fix").strip(" ,.")
    fix = re.sub(r"^আমি\s+(.+?)\s+(?:বলেছি|বললাম|বলছি|বুঝিয়েছি)$", r"\1", fix)  # Bangla: the verb comes last
    fix = re.sub(r"^(.+?)\s+(?:বলেছি|বললাম|বলছি)$", r"\1", fix)
    if not fix or len(fix.split()) > 8 or fix.lower() in _NOT_FIXES:
        return None
    return fix


def find_misheard(previous: str, fix: str) -> str | None:
    """The span of ``previous`` that the correction replaces: the words that sound most like ``fix``."""
    words = _WORD.findall(previous)
    size = len(_WORD.findall(fix))
    if not words or not size:
        return None
    best, best_score = None, 0.0
    joined_fix = fix.replace(" ", "")
    for length in range(max(1, size - 1), size + 3):  # "Spotify" can be heard as three words: "spot a fly"
        for start in range(0, len(words) - length + 1):
            span = " ".join(words[start:start + length])
            score = max(similarity(span, fix), similarity(span.replace(" ", ""), joined_fix))
            if score > best_score:
                best, best_score = span, score
    if best is None or best_score < 0.45 or best.lower() == fix.lower():
        return None
    return best


class Corrections:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / FILE
        self._items: dict[str, str] | None = None

    def items(self) -> dict[str, str]:
        if self._items is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._items = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._items = {}
        return self._items

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items(), ensure_ascii=False, indent=1), encoding="utf-8")

    def learn(self, heard: str, meant: str) -> None:
        items = self.items()
        items[heard.lower()] = meant
        while len(items) > MAX_CORRECTIONS:
            items.pop(next(iter(items)))
        self._save()

    def forget(self, heard: str | None = None) -> int:
        items = self.items()
        count = len(items) if heard is None else int(heard.lower() in items)
        if heard is None:
            items.clear()
        else:
            items.pop(heard.lower(), None)
        self._save()
        return count

    def apply(self, text: str) -> str:
        """Replace every learned mishearing (whole words, any case)."""
        for heard, meant in sorted(self.items().items(), key=lambda kv: -len(kv[0])):
            text = re.sub(rf"(?<![\wঀ-৿]){re.escape(heard)}(?![\wঀ-৿])", meant, text, flags=re.I)
        return text

    def vocabulary(self) -> list[str]:
        return list(dict.fromkeys(self.items().values()))
