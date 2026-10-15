"""Focus mode: "focus for 45 minutes".

While it runs, Jarvis keeps quiet: habit suggestions, PC-watcher notes and the weekly check wait, and
non-urgent alerts are held back (a critically low battery still speaks). Optionally the distracting
apps (FOCUS_APPS) are closed - through the normal close_application tool and its checks. At the end
you hear how long you focused and what was held back.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

DEFAULT_APPS = "Steam, Epic Games Launcher, Discord, WhatsApp, Telegram, Messenger"
MIN_MINUTES, MAX_MINUTES = 5, 240


@dataclass
class FocusSession:
    minutes: int
    started: float = field(default_factory=time.monotonic)
    held: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    @property
    def ends(self) -> float:
        return self.started + self.minutes * 60

    def left_minutes(self, now: float | None = None) -> int:
        return max(0, round((self.ends - (now or time.monotonic())) / 60))

    def over(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) >= self.ends

    def hold(self, message: str) -> None:
        if message and message not in self.held:
            self.held.append(message)

    def summary(self, now: float | None = None) -> str:
        spent = max(1, round(((now or time.monotonic()) - self.started) / 60))
        text = f"Focus time is over: {spent} minute{'s' if spent != 1 else ''}."
        if not self.held:
            return text + " Nothing came up while you worked."
        shown = "; ".join(m.rstrip(".") for m in self.held[:3])
        more = f" and {len(self.held) - 3} more" if len(self.held) > 3 else ""
        return text + f" I held back {len(self.held)} note{'s' if len(self.held) != 1 else ''}: {shown}{more}."


def clamp_minutes(value: int) -> int:
    return max(MIN_MINUTES, min(MAX_MINUTES, int(value)))


def app_list(setting: str) -> list[str]:
    return [a.strip() for a in (setting or "").replace(";", ",").split(",") if a.strip()]
