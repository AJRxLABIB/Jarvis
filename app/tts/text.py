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


_CLAUSE = re.compile(r"[,;:\u2014\u2013](?=\s)|\s[-\u2013\u2014]\s")


class SentenceSplitter:
    """Collects streamed text and returns complete sentences as soon as they end.

    ``quick_start``: the very first piece may end at a comma / dash once it is long enough (or at a space when a long
    sentence has no punctuation yet), so Jarvis starts talking while the model is still writing the first sentence."""

    FIRST_CLAUSE_CHARS = 12
    FIRST_FORCE_CHARS = 60

    def __init__(self, min_chars: int = 12, quick_start: bool = False) -> None:
        self.min_chars = min_chars
        self._buffer = ""
        self._first = quick_start

    def _first_piece(self) -> str | None:
        text = self._buffer
        if _BOUNDARY.search(text):
            return None                                   # a whole sentence is ready: the normal path takes it
        for match in _CLAUSE.finditer(text):
            if match.end() >= self.FIRST_CLAUSE_CHARS:
                piece, self._buffer = text[:match.end()].strip(), text[match.end():]
                return piece
        if len(text) >= self.FIRST_FORCE_CHARS:
            cut = text.rfind(" ", 0, self.FIRST_FORCE_CHARS - 10)
            if cut > self.FIRST_CLAUSE_CHARS:
                piece, self._buffer = text[:cut].strip(), text[cut:]
                return piece
        return None

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        sentences: list[str] = []
        if self._first:
            piece = self._first_piece()
            if piece:
                self._first = False
                sentences.append(piece)
        start = 0
        for match in _BOUNDARY.finditer(self._buffer):
            candidate = self._buffer[start:match.end()].strip()
            if len(candidate) < self.min_chars or candidate.lower().endswith(_ABBREVIATIONS):
                continue  # too short to be worth a separate utterance, or "e.g."
            sentences.append(candidate)
            self._first = False
            start = match.end()
        self._buffer = self._buffer[start:]
        return sentences

    def flush(self) -> str:
        rest, self._buffer = self._buffer.strip(), ""
        return rest
