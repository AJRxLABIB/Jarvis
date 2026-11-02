"""Safety controller: sits between gesture recognition and the OS. Fails closed, disabled by default, never lets a button stay pressed.

States: DISABLED, INITIALIZING, READY, ACTIVE, PAUSED, TRACKING_LOST, ERROR, SHUTTING_DOWN. Only ACTIVE turns gestures into mouse input.
emergency_stop() does not depend on hand recognition, the camera or the detector: it releases what we pressed and disables control."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

from app.gesture.cursor import VirtualCursor
from app.gesture.engine import GestureFrame

DISABLED, INITIALIZING, READY, ACTIVE, PAUSED, TRACKING_LOST, ERROR, SHUTTING_DOWN = (
    "DISABLED", "INITIALIZING", "READY", "ACTIVE", "PAUSED", "TRACKING_LOST", "ERROR", "SHUTTING_DOWN")
MAX_SCROLL_PER_FRAME = 4
MAX_ZOOM_PER_FRAME = 3
# a swipe of the open palm, as the user sees it, becomes one of these keys (off by default)
SWIPE_KEYS = {"browser": {"left": "alt+left", "right": "alt+right"}, "desktops": {"left": "ctrl+win+left", "right": "ctrl+win+right"},
              "slides": {"left": "left", "right": "right"}}


class InputDevice(Protocol):
    def move(self, x: int, y: int) -> None: ...
    def button(self, name: str, down: bool) -> None: ...
    def click(self, name: str, double: bool = False) -> None: ...
    def scroll(self, notches: int) -> None: ...
    def zoom(self, notches: int) -> None: ...
    def hotkey(self, combo: str) -> None: ...


class FakeInput:
    """Records what would have been sent. Used by tests and the no-control practice mode."""

    def __init__(self) -> None:
        self.log: list[tuple] = []
        self.fail = False

    def _do(self, *item) -> None:
        if self.fail:
            raise OSError("input failed")
        self.log.append(item)

    def move(self, x, y): self._do("move", x, y)
    def button(self, name, down): self._do("down" if down else "up", name)
    def click(self, name, double=False): self._do("double" if double else "click", name)
    def scroll(self, notches): self._do("scroll", notches)
    def zoom(self, notches): self._do("zoom", notches)
    def hotkey(self, combo): self._do("hotkey", combo)


class WindowsInput:
    """Real input through Win32 (hardware-unverified in CI; checked by `--validate`)."""

    def __init__(self) -> None:
        from app.tools import winapi
        self._w = winapi
        winapi.ensure_dpi_aware()      # coordinates must be physical pixels at any display scaling

    def move(self, x, y): self._w.mouse_move(x, y)
    def button(self, name, down): self._w.mouse_button(name, down)
    def click(self, name, double=False):
        self._w.mouse_button(name, True)
        self._w.mouse_button(name, False)
        if double:
            self._w.mouse_button(name, True)
            self._w.mouse_button(name, False)
    def scroll(self, notches): self._w.scroll(notches)
    def zoom(self, notches): self._w.zoom(notches)
    def hotkey(self, combo): self._w.press_combo(combo)


class SafetyController:
    def __init__(self, device: InputDevice, cursor: VirtualCursor, *, on_state: Callable[[str, str], None] | None = None,
                 right_click: bool = False, clock: Callable[[], float] = time.monotonic) -> None:
        self.device, self.cursor, self.on_state, self.clock = device, cursor, on_state, clock
        self.right_click = right_click
        self.zoom_enabled = True
        self.swipe_mode = "off"          # off | browser | desktops | slides
        self.macro_owned: set[str] = set()   # SWIPE_LEFT / SWIPE_RIGHT bound to a macro: the key pair stays quiet for them
        self.state = DISABLED
        self.reason = "disabled"
        self._pressed: set[str] = set()   # only buttons THIS controller pressed
        self._lock = threading.RLock()
        self.actions = 0
        self._frozen = False
        self.dispatch: Callable[[GestureFrame], None] | None = None   # mapping hook for non-mouse gestures

    # ---- state -----------------------------------------------------------------------------------------------------------------------------
    def _set(self, state: str, reason: str = "") -> None:
        if state != self.state:
            self.state, self.reason = state, reason
            if self.on_state:
                self.on_state(state, reason)

    def initializing(self) -> None:
        with self._lock:
            if self.state in (DISABLED, ERROR):
                self._set(INITIALIZING, "starting the camera and hand detector")

    def ready(self) -> None:
        with self._lock:
            if self.state == INITIALIZING:
                self._set(READY, "waiting for control to be turned on")

    def activate(self) -> bool:
        with self._lock:
            if self.state in (READY, PAUSED, TRACKING_LOST):
                self.cursor.reset()
                self._set(ACTIVE, "controlling the mouse")
                return True
            return False

    def pause(self, reason: str = "paused") -> None:
        with self._lock:
            if self.state in (ACTIVE, TRACKING_LOST):
                self.release_all()
                self._set(PAUSED, reason)

    def resume(self) -> bool:
        return self.activate() if self.state == PAUSED else False

    def fail(self, reason: str) -> None:
        with self._lock:
            self.release_all()
            self._set(ERROR, reason)

    def disable(self, reason: str = "turned off") -> None:
        with self._lock:
            self.release_all()
            self._set(DISABLED, reason)

    def emergency_stop(self) -> None:
        self.disable("emergency stop")

    def shutdown(self) -> None:
        with self._lock:
            self._set(SHUTTING_DOWN, "shutting down")
            self.release_all()
            self._set(DISABLED, "stopped")

    def release_all(self) -> None:
        """Release every button we pressed. Never raises: a failing device must not leave a button stuck in our bookkeeping."""
        for name in list(self._pressed):
            try:
                self.device.button(name, False)
            except Exception:
                pass
            self._pressed.discard(name)
        self.cursor.reset()

    @property
    def holding(self) -> bool:
        return bool(self._pressed)

    def tick(self) -> None:
        """~60 Hz: glide the pointer toward the goal so movement stays smooth even when the camera only gives 10-15 frames/s."""
        with self._lock:
            if self.state != ACTIVE:
                return
            try:
                spot = self.cursor.glide(self.clock())
                if spot:
                    self.device.move(*spot)
            except Exception as exc:
                self.fail(f"mouse input failed ({exc})")

    # ---- frames ----------------------------------------------------------------------------------------------------------------------------
    def process(self, frame: GestureFrame) -> None:
        with self._lock:
            if self.state == TRACKING_LOST and frame.gesture != "NONE":
                self._set(ACTIVE, "hand found again")
            if self.state not in (ACTIVE, TRACKING_LOST):
                return
            try:
                self._apply(frame)
            except Exception as exc:  # input failure: fail closed
                self.fail(f"mouse input failed ({exc})")

    def _apply(self, frame: GestureFrame) -> None:
        for event in frame.events:
            kind = event[0]
            if kind == "lost":
                self.release_all()
                self._set(TRACKING_LOST, "hand not visible")
                return
            if kind == "palm_hold":
                self.pause("open palm: paused")
                return
        # Aim: feed the hand position to the smoother (the 60 Hz tick() glides the pointer there). During a pinch the pointer is FROZEN at where you
        # aimed ~0.18 s before the fingers touched, so clicks land on target; while dragging it follows the hand again.
        if frame.pinch == "PINCH_STARTED" and not self.holding and frame.point is not None:
            spot = self.cursor.rewind(self.clock())
            if spot:
                self.device.move(*spot)
            self._frozen = True
        elif frame.pinch in ("NOT_PINCHED", "PINCH_RELEASED"):
            self._frozen = False
        if frame.point is not None and (not self._frozen or self.holding):
            self.cursor.aim(frame.point[0], frame.point[1], self.clock())
        for event in frame.events:
            kind = event[0]
            if kind == "click":
                self.device.click("left")
                self.actions += 1
            elif kind == "right_click":
                if self.right_click:
                    self.device.click("right")
                    self.actions += 1
            elif kind == "double_click":
                self.device.click("left", double=True)
                self.actions += 1
            elif kind == "drag_start":
                self.device.button("left", True)
                self._pressed.add("left")
            elif kind == "drag_end":
                if "left" in self._pressed:                 # only ever release a button WE pressed
                    self.device.button("left", False)
                    self._pressed.discard("left")
            elif kind == "zoom":
                if self.zoom_enabled:
                    self.device.zoom(max(-MAX_ZOOM_PER_FRAME, min(MAX_ZOOM_PER_FRAME, int(event[1]))))
                    self.actions += 1
            elif kind == "swipe":
                keys = SWIPE_KEYS.get(self.swipe_mode)
                if keys and event[1] in keys and f"SWIPE_{str(event[1]).upper()}" not in self.macro_owned:
                    self.device.hotkey(keys[event[1]])
                    self.actions += 1
            elif kind == "scroll":
                self.device.scroll(max(-MAX_SCROLL_PER_FRAME, min(MAX_SCROLL_PER_FRAME, int(event[1]))))
                self.actions += 1
        if self.dispatch:
            self.dispatch(frame)


class MappingRegistry:
    """Gesture -> an approved, registered JARVIS tool call. Off by default; each mapping has a cooldown, a permission class and a
    confirmation flag. The action runs through the normal ToolExecutor (permissions, confirmation, audit) - never a shell command."""

    from app.automation.tasks import FORBIDDEN_STEP_TOOLS as _F
    # run_workflow IS allowed here (a saved, reviewed workflow is the point of a macro; workflows cannot contain run_workflow, so no loops);
    # everything else automations may not use (schedules, forget_everything, ...) and every shell-like tool stays forbidden.
    FORBIDDEN = frozenset((_F - {"run_workflow"}) | {"run_command", "shell", "powershell", "python", "exec"})

    def __init__(self, known_tools=None, clock: Callable[[], float] = time.monotonic) -> None:
        self.known, self.clock = known_tools, clock
        self.items: dict[str, dict] = {}
        self._last: dict[str, float] = {}

    # Only gestures that do NOT already drive the mouse can trigger a macro: a pinch is a click, a point is the cursor, an open palm is
    # "pause", two fingers scroll. FIST needs a hold, so relaxing a hand cannot fire it.
    TRIGGERS = {"SWIPE_LEFT": 0.0, "SWIPE_RIGHT": 0.0, "FIST": 0.8}      # trigger -> default hold seconds (0 = the event itself)
    MAX_LEVEL = 2                                                          # up to DANGEROUS (always asks); never CRITICAL

    def add(self, gesture: str, tool: str, args: dict | None = None, *, cooldown_s: float = 2.0, confirm: bool = True, enabled: bool = True,
            profile: str = "*", hold_s: float | None = None, note: str = "", level: int = 0) -> None:
        if gesture not in self.TRIGGERS:
            raise ValueError(f"'{gesture}' cannot trigger a macro (it already moves the mouse or pauses control). Use: " + ", ".join(self.TRIGGERS))
        if tool in self.FORBIDDEN:
            raise ValueError(f"'{tool}' cannot be mapped to a gesture")
        if self.known is not None and tool not in self.known:
            raise ValueError(f"'{tool}' is not a registered tool")
        if level > self.MAX_LEVEL:
            raise ValueError(f"'{tool}' is too risky to run from a gesture")
        self.items[gesture] = {"tool": tool, "args": dict(args or {}), "cooldown_s": max(0.5, cooldown_s), "confirm": confirm, "enabled": enabled,
                               "profile": profile, "hold_s": self.TRIGGERS[gesture] if hold_s is None else max(0.0, hold_s), "note": note[:120]}

    def remove(self, gesture: str) -> bool:
        return self.items.pop(gesture, None) is not None

    def owned_swipes(self, profile: str = "*") -> set[str]:
        return {g for g, m in self.items.items() if g.startswith("SWIPE_") and m["enabled"] and m["profile"] in ("*", profile)}

    def due(self, gesture: str, profile: str = "*", held_s: float = 0.0) -> dict | None:
        item = self.items.get(gesture)
        if not item or not item["enabled"] or item["profile"] not in ("*", profile) or held_s < item["hold_s"]:
            return None
        now = self.clock()
        if now - self._last.get(gesture, -1e9) < item["cooldown_s"]:
            return None
        self._last[gesture] = now
        return item
