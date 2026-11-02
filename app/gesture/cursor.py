"""Virtual cursor: camera position -> screen pixels. Mirroring, margins (an active sub-rectangle of the image, so the screen edges are
reachable without leaving the camera view), calibration, EMA smoothing, dead zone, speed limit and clamping. Pure; no OS calls."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass


@dataclass
class Calibration:
    """The part of the camera image (normalised) that maps to the whole screen. Default: the middle 70%."""
    left: float = 0.15
    top: float = 0.15
    right: float = 0.85
    bottom: float = 0.85

    def valid(self) -> bool:
        return 0 <= self.left < self.right <= 1 and 0 <= self.top < self.bottom <= 1 and (self.right - self.left) >= 0.2 and (self.bottom - self.top) >= 0.2

    @staticmethod
    def from_corners(top_left: tuple[float, float], bottom_right: tuple[float, float]) -> Calibration | None:
        a, b = top_left, bottom_right
        cal = Calibration(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        return cal if cal.valid() else None


@dataclass
class CursorConfig:
    mirror: bool = True
    smoothing: float = 0.35        # EMA weight of the NEW sample (lower = steadier but laggier)
    dead_zone_px: float = 3.0
    max_speed_px_s: float = 6000.0
    adaptive: bool = True          # fast movement smooths less, slow movement smooths more


class OneEuro:
    """One Euro filter (Casiez et al.): steady when the hand rests, quick when it moves. Works at any (even irregular, low) frame rate."""

    def __init__(self, min_cutoff: float = 0.7, beta: float = 8.0, d_cutoff: float = 1.0) -> None:
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x: float | None = None
        self.dx = 0.0
        self.t = 0.0

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / max(dt, 1e-4))

    def reset(self) -> None:
        self.x, self.dx = None, 0.0

    def __call__(self, value: float, now: float) -> float:
        if self.x is None:
            self.x, self.dx, self.t = value, 0.0, now
            return value
        dt = max(1e-3, now - self.t)
        self.t = now
        d = (value - self.x) / dt
        self.dx += self._alpha(self.d_cutoff, dt) * (d - self.dx)
        cutoff = self.min_cutoff + self.beta * abs(self.dx)
        self.x += self._alpha(cutoff, dt) * (value - self.x)
        return self.x


class VirtualCursor:
    def __init__(self, screen: tuple[int, int, int, int], cfg: CursorConfig | None = None, calibration: Calibration | None = None) -> None:
        self.cfg, self.cal = cfg or CursorConfig(), calibration or Calibration()
        self.set_screen(screen)
        self.pos: tuple[float, float] | None = None
        self._t: float | None = None
        self._fx, self._fy = OneEuro(), OneEuro()
        self._hist: deque = deque(maxlen=64)
        self.goal: tuple[float, float] | None = None
        self._unplaced = False

    def set_screen(self, rect: tuple[int, int, int, int]) -> None:
        """rect = (left, top, right, bottom) in physical pixels (a monitor, or the whole virtual desktop)."""
        x0, y0, x1, y1 = rect
        if x1 <= x0 or y1 <= y0:
            raise ValueError("empty screen rectangle")
        self.screen = rect

    def reset(self) -> None:
        self.pos, self._t = None, None
        self.clear_motion()

    def target(self, nx: float, ny: float) -> tuple[float, float]:
        if self.cfg.mirror:
            nx = 1.0 - nx
        cal = self.cal
        if self.cfg.mirror:  # calibration is stored in image coordinates as the user sees the mirrored preview
            u = (nx - (1 - cal.right)) / (cal.right - cal.left)
        else:
            u = (nx - cal.left) / (cal.right - cal.left)
        v = (ny - cal.top) / (cal.bottom - cal.top)
        x0, y0, x1, y1 = self.screen
        return (x0 + min(1.0, max(0.0, u)) * (x1 - x0 - 1), y0 + min(1.0, max(0.0, v)) * (y1 - y0 - 1))

    def move(self, nx: float, ny: float, now: float) -> tuple[int, int] | None:
        """Return the new screen pixel, or None when the move is inside the dead zone."""
        tx, ty = self.target(nx, ny)
        if self.pos is None:
            self.pos, self._t = (tx, ty), now
            return int(tx), int(ty)
        px, py = self.pos
        dx, dy = tx - px, ty - py
        distance = (dx * dx + dy * dy) ** 0.5
        if distance < self.cfg.dead_zone_px:
            self._t = now
            return None
        alpha = self.cfg.smoothing
        if self.cfg.adaptive:
            alpha = min(1.0, max(alpha, alpha + distance / 800.0))
        step_x, step_y = dx * alpha, dy * alpha
        dt = max(1e-3, now - (self._t or now))
        limit = self.cfg.max_speed_px_s * dt
        step = (step_x * step_x + step_y * step_y) ** 0.5
        if step > limit > 0:
            step_x, step_y = step_x * limit / step, step_y * limit / step
        x0, y0, x1, y1 = self.screen
        nxp, nyp = min(x1 - 1, max(x0, px + step_x)), min(y1 - 1, max(y0, py + step_y))
        self.pos, self._t = (nxp, nyp), now
        return int(round(nxp)), int(round(nyp))

    def aim(self, nx: float, ny: float, now: float) -> None:
        """Feed one hand position (normalised image coords). Sets the screen goal; call glide() at ~60 Hz to move toward it."""
        gx, gy = self.target(self._fx(nx, now), self._fy(ny, now))
        self.goal = (gx, gy)
        self._hist.append((now, gx, gy))
        if self.pos is None:
            self.pos, self._t, self._unplaced = (gx, gy), now, True

    def glide(self, now: float) -> tuple[int, int] | None:
        """Move the pointer part of the way to the goal (exponential approach, speed cap, dead zone). Returns pixels or None when nothing changes."""
        goal = self.goal
        if goal is None or self.pos is None:
            return None
        if self._unplaced:                      # first position after start: put the pointer there (no glide from wherever it was)
            self._unplaced = False
            return int(round(self.pos[0])), int(round(self.pos[1]))
        last = self._t or now
        dt = min(0.1, max(1e-3, now - last))
        px, py = self.pos
        dx, dy = goal[0] - px, goal[1] - py
        dist_px = math.hypot(dx, dy)
        if dist_px < min(self.cfg.dead_zone_px, 1.5):
            self._t = now
            return None
        tau = 0.02 + 0.2 * self.cfg.smoothing            # seconds: bigger smoothing = softer, slower follow
        k = 1.0 - math.exp(-dt / tau)
        sx, sy = dx * k, dy * k
        step = math.hypot(sx, sy)
        limit = self.cfg.max_speed_px_s * dt
        if step > limit > 0:
            sx, sy = sx * limit / step, sy * limit / step
        x0, y0, x1, y1 = self.screen
        nxp, nyp = min(x1 - 1, max(x0, px + sx)), min(y1 - 1, max(y0, py + sy))
        moved = (int(round(nxp)), int(round(nyp))) != (int(round(px)), int(round(py)))
        self.pos, self._t = (nxp, nyp), now
        return (int(round(nxp)), int(round(nyp))) if moved else None

    def rewind(self, now: float, seconds: float = 0.18) -> tuple[int, int] | None:
        """Where the hand was `seconds` ago: used at the start of a pinch so the click lands where you AIMED, not where the fingers drifted."""
        hist = self._hist
        if not hist:
            return None
        chosen = hist[0]
        for entry in reversed(hist):
            if entry[0] <= now - seconds:
                chosen = entry
                break
        _, gx, gy = chosen
        self.goal, self.pos = (gx, gy), (gx, gy)
        self._t = now
        return int(round(gx)), int(round(gy))

    def clear_motion(self) -> None:
        self._fx.reset()
        self._fy.reset()
        self._hist.clear()
        self.goal = None
        self._unplaced = False
