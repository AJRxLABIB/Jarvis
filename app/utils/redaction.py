"""Secret redaction for logs and any other text that is written to disk.

Two layers:
* key-based: values stored under keys such as ``password`` or ``api_key``
* pattern-based: well-known token formats and ``key=value`` pairs inside text
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"
_MAX_DEPTH = 8

_SENSITIVE_PARTS = frozenset(
    {
        "password", "passwd", "passphrase", "pwd", "secret", "token", "apikey",
        "authorization", "credential", "credentials", "cookie", "otp", "pin",
    }
)
_SENSITIVE_PAIRS = frozenset(
    {("api", "key"), ("private", "key"), ("access", "key"), ("secret", "key"), ("client", "secret")}
)
# "token_count", "password_type" ... describe a secret without containing one.
_BENIGN_SUFFIXES = frozenset(
    {"count", "counts", "len", "length", "limit", "limits", "type", "usage", "budget", "size", "max", "min"}
)

_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), REDACTED),
    # credentials embedded in URLs: scheme://user:pass@host
    (re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), r"\g<scheme>" + REDACTED + "@"),
    # HTTP auth headers
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]{8,}=*"), r"\1 " + REDACTED),
    # key=value / key: value / "key": "value"
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(api[_\-]?key|access[_\-]?key|secret[_\-]?key|client[_\-]?secret|private[_\-]?key|"
            r"auth[_\-]?token|access[_\-]?token|refresh[_\-]?token|token|secret|password|passwd|pwd|passphrase)"
            r"(\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"
        ),
        r"\1\2" + REDACTED,
    ),
    # natural language: "my password is hunter2"
    (re.compile(r"(?i)\b((?:password|passcode|passphrase|pin)\s+(?:is|was)\s+)\S+"), r"\1" + REDACTED),
    # well-known token formats
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), REDACTED),
    (re.compile(r"\bya29\.[A-Za-z0-9_\-]{20,}"), REDACTED),                    # Google access token
    (re.compile(r"(?<![A-Za-z0-9])1//[A-Za-z0-9_\-]{30,}"), REDACTED),           # Google refresh token
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{20,}"), REDACTED),                   # Google OAuth client secret
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), REDACTED),  # JWT
)

_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SPLIT = re.compile(r"[^a-z0-9]+")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _redact_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group())
    if 13 <= len(digits) <= 19 and _luhn_valid(digits):
        return REDACTED
    return match.group()


def is_sensitive_key(key: str) -> bool:
    """True if a mapping key suggests its value is a secret."""
    parts = [p for p in _KEY_SPLIT.split(_CAMEL_BOUNDARY.sub("_", str(key)).lower()) if p]
    if not parts or parts[-1] in _BENIGN_SUFFIXES:
        return False
    if any(part in _SENSITIVE_PARTS for part in parts):
        return True
    return any(pair in _SENSITIVE_PAIRS for pair in zip(parts, parts[1:]))


def redact(text: str) -> str:
    """Return ``text`` with secrets replaced by ``[REDACTED]``."""
    if not text:
        return text
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return _CARD_CANDIDATE.sub(_redact_card, text)


def redact_value(value: Any, key: str | None = None, _depth: int = 0) -> Any:
    """Recursively redact strings, mappings and sequences."""
    if key is not None and is_sensitive_key(key) and value not in (None, ""):
        return REDACTED
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {k: redact_value(v, key=str(k), _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(v, _depth=_depth + 1) for v in value]
    return value
