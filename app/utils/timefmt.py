"""Times the way people say them: 1:38 PM, not 13:38 (12-hour clock, no leading zero)."""

from __future__ import annotations

from datetime import datetime


def time12(at: datetime, seconds: bool = False) -> str:
    text = at.strftime("%I:%M:%S %p" if seconds else "%I:%M %p")
    return text.lstrip("0")
