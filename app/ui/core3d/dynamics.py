"""The shared animation state machine for the 3D core.

Every visual parameter (spin speed, brightness, shell breathing, convergence, scan sweep, outward pulse, tint) is eased
toward the target of the current Jarvis state, so a state change is a smooth glide of 0.35-0.8 s, never a jump, and the
scene is never rebuilt. The shader clock (``phase``) is the integral of the eased speed, so speeding up or slowing down
never makes the animation jump backwards or blur.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# state -> targets.  speed: spin/orbit multiplier; bright: core brightness; breath: shell breathing amplitude;
# converge: particles pulled toward the nucleus; scan: scanning-arc strength; pulse: outward pulse strength (speech /
# execution); tint: rgb colour of the core energy.
CYAN, VIOLET, AMBER, RED, TEAL = (0.29, 0.91, 1.0), (0.65, 0.42, 1.0), (1.0, 0.77, 0.42), (1.0, 0.40, 0.48), (0.33, 0.96, 0.82)

TARGETS: dict[str, dict[str, object]] = {
    "IDLE":                   dict(speed=1.0, bright=0.85, breath=0.020, converge=0.0, scan=0.15, pulse=0.0, tint=CYAN, label="READY"),
    "LISTENING":              dict(speed=1.35, bright=1.10, breath=0.045, converge=0.0, scan=0.25, pulse=0.0, tint=CYAN, label="LISTENING"),
    "RECOGNIZING":            dict(speed=1.5, bright=1.05, breath=0.020, converge=0.55, scan=0.9, pulse=0.0, tint=TEAL, label="RECOGNIZING"),
    "PROCESSING":             dict(speed=2.6, bright=1.30, breath=0.015, converge=0.30, scan=1.0, pulse=0.0, tint=VIOLET, label="PROCESSING"),
    "EXECUTING":              dict(speed=2.2, bright=1.25, breath=0.015, converge=0.15, scan=0.6, pulse=0.8, tint=TEAL, label="EXECUTING"),
    "SPEAKING":               dict(speed=1.5, bright=1.12, breath=0.030, converge=0.0, scan=0.2, pulse=1.0, tint=CYAN, label="SPEAKING"),
    "WAITING_FOR_PERMISSION": dict(speed=0.7, bright=0.95, breath=0.030, converge=0.0, scan=0.1, pulse=0.35, tint=AMBER, label="WAITING"),
    "ERROR":                  dict(speed=0.35, bright=0.75, breath=0.010, converge=0.0, scan=0.0, pulse=0.0, tint=RED, label="ERROR"),
}
ALIASES = {"THINKING": "PROCESSING", "STARTING": "IDLE", "READY": "IDLE"}
MAX_SPEED = 3.0           # above this the structure would smear into a blur


def canonical(state: str) -> str:
    state = ALIASES.get(state, state)
    return state if state in TARGETS else "IDLE"


def _ease(current: float, target: float, dt: float, seconds: float) -> float:
    """Exponential approach: ~95% of the way after ``seconds``."""
    k = 1.0 - math.exp(-3.0 * dt / max(seconds, 1e-3))
    return current + (target - current) * k


@dataclass
class CoreDynamics:
    state: str = "IDLE"
    speed_scale: float = 1.0          # user setting "core animation speed"
    glow_scale: float = 1.0           # user setting "glow intensity"
    reduced_motion: bool = False
    phase: float = 0.0                # shader clock (integral of speed)
    clock: float = 0.0                # plain seconds
    speed: float = 1.0
    bright: float = 0.85
    breath: float = 0.02
    converge: float = 0.0
    scan: float = 0.15
    pulse: float = 0.0
    sweep: float = 0.0                # scanning-arc angle (radians)
    level: float = 0.0                # smoothed microphone / speech level 0..1
    tint: tuple[float, float, float] = field(default_factory=lambda: CYAN)
    _pulse_clock: float = 0.0

    def set_state(self, state: str) -> None:
        self.state = canonical(state)

    @property
    def label(self) -> str:
        return str(TARGETS[self.state]["label"])

    def step(self, dt: float, level: float | None = None) -> None:
        """Advance by ``dt`` seconds (frame-rate independent). ``level`` is 0..1 audio level, if known."""
        dt = max(0.0, min(dt, 0.25))      # a long pause must not make a huge jump
        target = TARGETS[self.state]
        fast = 0.45 if target["speed"] >= self.speed else 0.9     # speed up quickly, relax slowly
        motion = 0.25 if self.reduced_motion else 1.0
        wanted_speed = min(MAX_SPEED, float(target["speed"]) * self.speed_scale) * motion
        self.speed = _ease(self.speed, wanted_speed, dt, fast)
        self.bright = _ease(self.bright, float(target["bright"]), dt, 0.5)
        self.breath = _ease(self.breath, float(target["breath"]) * (0.0 if self.reduced_motion else 1.0), dt, 0.6)
        self.converge = _ease(self.converge, float(target["converge"]), dt, 0.5)
        self.scan = _ease(self.scan, float(target["scan"]) * (0.4 if self.reduced_motion else 1.0), dt, 0.5)
        self.pulse = _ease(self.pulse, float(target["pulse"]), dt, 0.5)
        tint = target["tint"]
        self.tint = tuple(_ease(c, t, dt, 0.6) for c, t in zip(self.tint, tint))     # type: ignore[arg-type]
        self.clock += dt
        self.phase += dt * self.speed
        self.sweep = (self.sweep + dt * (0.8 + 2.2 * self.speed) * (0.0 if self.reduced_motion else 1.0)) % (2 * math.pi)
        if level is not None:
            self.level = self.level * 0.6 + max(0.0, min(1.0, level)) * 0.4
        else:
            self.level *= math.exp(-dt * 2.5)
        if self.state == "SPEAKING" and level is None:
            # no measured audio while speaking: a clearly synthetic speech cadence (not claimed to match the audio)
            t = self.clock
            self.level = 0.30 + 0.30 * abs(math.sin(t * 6.3)) * abs(math.sin(t * 2.1 + 1.0))
        self._pulse_clock = (self._pulse_clock + dt * 0.9) % 1.0

    @property
    def pulse_phase(self) -> float:
        return self._pulse_clock
