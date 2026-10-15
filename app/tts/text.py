"""Preparing text for speech: markdown clean-up and sentence splitting for streaming."""

from __future__ import annotations

import re

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_MARKUP = re.compile(r"[*_#>~|]+")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_SPACES = re.compile(r"\s+")
# sentence end: . ! ? followed by whitespace, or a line break
_BOUNDARY = re.compile(r"(?<=[.!?।])[\"')\]]*\s+|\n+")
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "mr.", "mrs.", "ms.", "dr.", "vs.", "no.")


def clean_for_speech(text: str) -> str:
    text = _CODE_BLOCK.sub(" (code omitted) ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("the link", text)
    text = _BULLET.sub("", text)
    text = _MARKUP.sub("", text)
    text = _EMOJI.sub("", text)
    return _SPACES.sub(" ", text).strip()


class SentenceSplitter:
    """Collects streamed text and returns complete sentences as soon as they end."""

    def __init__(self, min_chars: int = 12) -> None:
        self.min_chars = min_chars
        self._buffer = ""

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        sentences: list[str] = []
        start = 0
        for match in _BOUNDARY.finditer(self._buffer):
            candidate = self._buffer[start:match.end()].strip()
            if len(candidate) < self.min_chars or candidate.lower().endswith(_ABBREVIATIONS):
                continue  # too short to be worth a separate utterance, or "e.g."
            sentences.append(candidate)
            start = match.end()
        self._buffer = self._buffer[start:]
        return sentences

    def flush(self) -> str:
        rest, self._buffer = self._buffer.strip(), ""
        return rest
