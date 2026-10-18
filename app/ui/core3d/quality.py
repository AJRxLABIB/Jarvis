"""Rendering quality levels and the automatic governor."""

from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("low", "balanced", "high")


@dataclass(frozen=True)
class Quality:
    name: str
    nucleus: int          # particles in the inner nucleus
    shell: int            # particles in the outer shell
    filaments: int        # neural filament curves
    filament_segments: int
    rings: int            # orbital rings
    bloom: bool           # selective glow post-process
    bands: int            # glowing energy ribbons wrapped around the sphere
    band_points: int
    fps: int              # frame-rate target (paced to the display: 72 on a 144 Hz screen, 60 on a 60 Hz one)
    samples: int          # multisample anti-aliasing (0 = off)
    supersample: float = 1.0   # drawn this many times larger, then filtered down: finer, cleaner dots and lines


QUALITY: dict[str, Quality] = {
    "low": Quality("low", 1600, 2600, 150, 22, 5, False, 3, 260, 30, 0),
    "balanced": Quality("balanced", 4200, 9000, 480, 30, 7, True, 6, 520, 60, 4, 1.25),
    "high": Quality("high", 9000, 18000, 1000, 38, 9, True, 8, 800, 120, 4, 1.5),
}


def resolve(setting: str, auto_level: str = "high") -> Quality:
    """``setting``: low | balanced | high | auto (auto uses the governor's current level)."""
    name = auto_level if setting == "auto" else setting
    return QUALITY.get(name, QUALITY["balanced"])


class Governor:
    """Automatic quality, judged on what a frame COSTS to draw (not on the gap between frames, which is set by the
    frame-rate target). Steps down one level when frames use most of their time budget; steps up again only after a
    long stretch of plenty of headroom, never back to a level it already had to leave, and waits ``cooldown`` seconds
    between changes - so it can't oscillate."""

    def __init__(self, start: str = "high", window: float = 3.0, cooldown: float = 15.0, heavy: float = 0.75,
                 light: float = 0.30) -> None:
        self.level = start if start in LEVELS else "high"
        self.window, self.cooldown, self.heavy, self.light = window, cooldown, heavy, light
        self._cost = 0.0
        self._budget = 0.0
        self._frames = 0
        self._elapsed = 0.0
        self._since_change = cooldown
        self._light_windows = 0
        self._ceiling = len(LEVELS) - 1          # the highest level still allowed

    def frame(self, cost: float, budget: float, dt: float | None = None) -> str | None:
        """One frame: ``cost`` seconds spent drawing it, ``budget`` seconds allowed at the target rate.
        Returns the new level when it changed, else None."""
        dt = budget if dt is None else dt
        if cost < 0 or budget <= 0 or dt > 1.0:      # a pause (hidden window, breakpoint) is not a slow frame
            return None
        self._cost += cost
        self._budget += budget
        self._frames += 1
        self._elapsed += dt
        self._since_change += dt
        if self._elapsed < self.window:
            return None
        load = self._cost / self._budget
        self._cost = self._budget = self._elapsed = 0.0
        self._frames = 0
        index = LEVELS.index(self.level)
        if self._since_change < self.cooldown:
            return None
        if load > self.heavy and index > 0:
            self._ceiling = index - 1                  # never climb back to the level that was too heavy
            self.level = LEVELS[index - 1]
            self._since_change = 0.0
            self._light_windows = 0
            return self.level
        if load < self.light and index < self._ceiling:
            self._light_windows += 1
            if self._light_windows >= 3:               # about 10 s of clear headroom
                self.level = LEVELS[index + 1]
                self._since_change = 0.0
                self._light_windows = 0
                return self.level
        else:
            self._light_windows = 0
        return None
