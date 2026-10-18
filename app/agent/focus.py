"""Focus mode: "focus for 45 minutes".

While it runs, Jarvis keeps quiet: habit suggestions, PC-watcher notes and the weekly check wait, and
non-urgent alerts are held back (a critically low battery still speaks). Optionally the distracting
apps (FOCUS_APPS) are closed - through the normal close_application tool and its checks. At the end
you hear how long you focused and what was held back.

Keep-closed (``keep_closed``, asked for when you start focus, with the close-distractions choice): every 30 seconds the
blocked apps that are running again are closed again - at most 3 times per app per session, never twice within a minute -
so a chat app that reopens itself doesn't undo your focus, and Jarvis never gets into a fight with you if you open one on
purpose. The end-of-session summary says how often each was closed.
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
    keep_closed: bool = False
    blocked: list[str] = field(default_factory=list)
    reclosed: dict[str, int] = field(default_factory=dict)
    _last_close: dict[str, float] = field(default_factory=dict)

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

    MAX_RECLOSE = 3
    MIN_GAP = 60.0

    def guard_candidates(self, process_names: list[str], now: float | None = None) -> list[str]:
        """Blocked apps that are running right now and may be closed again (None when keep-closed is off)."""
        if not self.keep_closed or self.over(now):
            return []
        moment = now or time.monotonic()
        running = {n.lower().removesuffix(".exe").replace(" ", "") for n in process_names}
        out = []
        for app in self.blocked:
            key = app.lower().replace(" ", "")
            if not any(key in r or r in key and len(r) > 3 for r in running):
                continue
            if self.reclosed.get(app, 0) >= self.MAX_RECLOSE or moment - self._last_close.get(app, -1e9) < self.MIN_GAP:
                continue
            out.append(app)
        return out

    def note_reclosed(self, app: str, now: float | None = None) -> None:
        self.reclosed[app] = self.reclosed.get(app, 0) + 1
        self._last_close[app] = now or time.monotonic()

    def summary(self, now: float | None = None) -> str:
        spent = max(1, round(((now or time.monotonic()) - self.started) / 60))
        text = f"Focus time is over: {spent} minute{'s' if spent != 1 else ''}."
        again = sum(self.reclosed.values())
        if again:
            names = ", ".join(f"{a} x{n}" for a, n in self.reclosed.items())
            text += f" I closed distracting apps again {again} time{'s' if again != 1 else ''} ({names})."
        if not self.held:
            return text + (" Nothing else came up while you worked." if again else " Nothing came up while you worked.")
        shown = "; ".join(m.rstrip(".") for m in self.held[:3])
        more = f" and {len(self.held) - 3} more" if len(self.held) > 3 else ""
        return text + f" I held back {len(self.held)} note{'s' if len(self.held) != 1 else ''}: {shown}{more}."


def clamp_minutes(value: int) -> int:
    return max(MIN_MINUTES, min(MAX_MINUTES, int(value)))


def app_list(setting: str) -> list[str]:
    return [a.strip() for a in (setting or "").replace(";", ",").split(",") if a.strip()]
