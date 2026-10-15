"""Motion for the HUD: one shared clock and smooth values.

Everything animated asks the one ``CLOCK`` for ticks; the timer only runs while something is actually
moving (a panel drawing itself in, a meter gliding to a new value, the background scan), so an idle
window costs nothing. ANIMATIONS=false (or "turn off animations") switches it all off: widgets then
paint their final state at once. The reactor orb in the middle has its own animation and is not
touched here.
"""

from __future__ import annotations

import math
import time
from typing import Callable

ENABLED = True  # set from the ANIMATIONS setting when the window starts
FPS = 30


def set_enabled(on: bool) -> None:
    global ENABLED
    ENABLED = bool(on)


def ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


class Tween:
    """A value that glides to its target (exponential ease, ~``seconds`` to get there)."""

    def __init__(self, value: float | None = None, seconds: float = 0.45) -> None:
        self.value = value
        self.target = value
        self.seconds = seconds
        self._last = time.monotonic()

    def set(self, target: float | None) -> None:
        if target is None or self.value is None or not ENABLED:
            self.value = target
        self.target = target
        self._last = time.monotonic()

    @property
    def moving(self) -> bool:
        return self.value is not None and self.target is not None and abs(self.value - self.target) > 0.05

    def step(self, now: float | None = None) -> float | None:
        now = now or time.monotonic()
        dt, self._last = now - self._last, now
        if self.moving:
            k = 1 - math.exp(-dt * 5.0 / max(0.05, self.seconds))
            self.value += (self.target - self.value) * k  # type: ignore[operator]
            if not self.moving:
                self.value = self.target
        return self.value


class Clock:
    """Calls ``tick(now)`` on subscribers ~30 times a second while any of them says it is still moving."""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[float], bool]] = []
        self._timer = None

    def join(self, tick: Callable[[float], bool]) -> None:
        """``tick(now) -> still moving``. Starts the timer if needed (no-op without a Qt app or when disabled)."""
        if not ENABLED:
            return
        if tick not in self._subscribers:
            self._subscribers.append(tick)
        self._ensure_running()

    def _ensure_running(self) -> None:
        try:
            from PySide6.QtCore import QCoreApplication, QTimer
        except Exception:
            return
        if QCoreApplication.instance() is None:
            return
        if self._timer is None:
            from PySide6.QtCore import Qt

            self._timer = QTimer()
            self._timer.setTimerType(Qt.TimerType.PreciseTimer)  # even frame spacing
            self._timer.setInterval(int(1000 / FPS))
            self._timer.timeout.connect(self._tick)
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self) -> None:
        now = time.monotonic()
        alive = []
        for tick in list(self._subscribers):
            try:
                if tick(now):
                    alive.append(tick)
            except RuntimeError:  # the widget was deleted
                continue
            except Exception:  # one broken widget must never stop every other animation
                import logging

                logging.getLogger("jarvis.ui.motion").warning("an animation failed and was stopped", exc_info=True)
                continue
        self._subscribers = alive
        if not alive and self._timer is not None:
            self._timer.stop()

    @property
    def running(self) -> bool:
        return bool(self._timer is not None and self._timer.isActive())


CLOCK = Clock()
