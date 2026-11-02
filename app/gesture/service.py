"""Gesture service: camera lease -> detector -> engine -> safety controller, on one worker thread. Works without the LLM.

Nothing starts by itself: enable() is the only way the camera opens for gestures, and every exit path (disable, emergency stop, camera
loss, detector failure, shutdown) goes through the safety controller so no mouse button is left pressed. Only measured numbers are
reported (frames, fps, detection latency); nothing is estimated."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from app.gesture import camera as cam
from app.gesture import safety as sf
from app.gesture.cursor import Calibration, CursorConfig, VirtualCursor
from app.gesture.engine import GestureConfig, MultiHandEngine
from app.utils.logging_setup import get_logger

_log = get_logger("gesture")
DEFAULT_SCREEN = (0, 0, 1920, 1080)
REACH = {"small": (0.3, 0.3, 0.7, 0.7), "medium": (0.2, 0.2, 0.8, 0.8), "large": (0.1, 0.1, 0.9, 0.9)}
SMOOTH = {"smoother": 0.6, "balanced": 0.35, "quicker": 0.12}


@dataclass
class GestureSettings:
    fps: float = 30.0
    max_hands: int = 2          # two hands: one points, two pinching hands zoom
    zoom: bool = True
    swipe: str = "off"         # off | browser | desktops | slides (an open-palm swipe sends the key pair)
    dwell_click: bool = False  # hold still to click (accessibility)
    mirror: bool = True
    smoothing: float = 0.35
    drag: bool = False
    fist_drag: bool = False
    double_click: bool = False
    right_click: bool = False
    detection_confidence: float = 0.6
    min_confidence: float = 0.6
    preview: bool = False
    monitor: int = 0           # 0 = primary
    calibration: tuple = (0.2, 0.2, 0.8, 0.8)
    reach: str = "medium"      # how far your hand travels to cross the screen: small | medium | large | custom
    smooth: str = "balanced"   # smoother | balanced | quicker (sets smoothing)
    auto_profile: bool = False   # switch profile by the foreground app (browser -> browsing ...); off = only suggested


@dataclass
class Stats:
    frames: int = 0
    detected: int = 0
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=120))
    started: float = 0.0
    last_gesture: str = "NONE"

    def summary(self) -> dict:
        lat = sorted(self.latencies_ms)
        elapsed = max(1e-6, time.monotonic() - self.started) if self.started else 0
        return {"frames_processed": self.frames, "frames_with_hand": self.detected,
                "fps_measured": round(self.frames / elapsed, 1) if elapsed else None,
                "detect_ms_median": round(lat[len(lat) // 2], 1) if lat else None,
                "detect_ms_p95": round(lat[int(len(lat) * 0.95) - 1], 1) if len(lat) >= 20 else None}


class GestureService:
    def __init__(self, *, broker_factory: Callable[[], cam.CameraBroker] = cam.shared_broker,
                 detector_factory: Callable[[GestureSettings], object] | None = None,
                 device_factory: Callable[[], sf.InputDevice] = sf.WindowsInput,
                 screen_provider: Callable[[int], tuple[int, int, int, int]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.broker_factory, self.detector_factory, self.device_factory = broker_factory, detector_factory, device_factory
        self.screen_provider, self.clock = screen_provider or _screen, clock
        self.settings = GestureSettings()
        self.controller: sf.SafetyController | None = None
        self.mapping = sf.MappingRegistry()
        self.run_action: Callable[[str, dict, bool], None] | None = None   # (tool, args, confirm) -> ToolExecutor, set by the app
        self.on_state: Callable[[str, str], None] | None = None
        self.on_frame: Callable[[object, object], None] | None = None      # (hand, GestureFrame) for the optional preview
        self.stats = Stats()
        self.problem = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._atexit = False
        self._engine = None
        self.profile = "general"                 # app/gesture/profiles.py: the mode shown to the user
        self.store = None                        # MacroStore: set by the app so profile + macros survive restarts
        self.foreground_probe: Callable[[], str] | None = None   # () -> foreground process name (for auto_profile / suggestions)
        self.limits_provider: Callable[[], tuple[float, int]] | None = None   # () -> (max frames/s, max hands) from the performance mode
        self.fps_cap = 0.0                       # 0 = no cap (what is in force now; shown in status)
        self.suggested = ""                      # a profile the foreground app suggests (shown, applied only if auto_profile)
        self._fist_since: float | None = None
        self.last_macro = ""                     # last macro (or, in practice, the one that WOULD run), for the status line
        self.practice = False    # practice mode: gestures are recognised and shown, no mouse input is ever sent

    # ---- state -----------------------------------------------------------------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self.controller.state if self.controller else sf.DISABLED

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def configure(self, **changes) -> GestureSettings:
        for key, value in changes.items():
            if not hasattr(self.settings, key):
                raise ValueError(f"unknown gesture setting '{key}'")
            setattr(self.settings, key, value)
        s = self.settings
        s.smoothing = min(0.9, max(0.05, float(s.smoothing)))
        s.max_hands = max(1, min(2, int(s.max_hands)))
        s.fps = max(5.0, min(60.0, float(s.fps)))
        if not Calibration(*s.calibration).valid():
            s.calibration = GestureSettings().calibration
        if "reach" in changes and changes["reach"] in REACH:
            s.calibration = REACH[changes["reach"]]
        if "smooth" in changes and changes["smooth"] in SMOOTH:
            s.smoothing = SMOOTH[changes["smooth"]]
        self._apply_live()
        return s

    def _apply_live(self) -> None:
        """Changes take effect at once while running (no restart)."""
        ctl, s = self.controller, self.settings
        if ctl is not None and self.running:
            ctl.cursor.cfg.smoothing, ctl.cursor.cfg.mirror = s.smoothing, s.mirror
            ctl.cursor.cal = Calibration(*s.calibration)
            ctl.right_click, ctl.zoom_enabled, ctl.swipe_mode = s.right_click, s.zoom, s.swipe
            if self._engine is not None:
                c = self._engine.cfg
                c.zoom, c.dwell_click, c.mirror, c.drag_enabled = s.zoom and s.max_hands >= 2, s.dwell_click, s.mirror, s.drag
                c.right_click, c.double_click, c.fist_drag = s.right_click, s.double_click, s.fist_drag

    # ---- profiles + macros -------------------------------------------------------------------------------------------------------------------
    def apply_profile(self, name: str, *, save: bool = True) -> str:
        from app.gesture import profiles

        changes = profiles.settings_for(name)            # ValueError for an unknown name
        if self.settings.reach == "custom":
            changes.pop("reach", None)                   # keep a calibration the user made by hand
        self.configure(**changes)
        self.profile = name
        self._sync_macros()
        if save:
            self.save_store()
        return profiles.describe(name)

    def _sync_macros(self) -> None:
        if self.controller is not None:
            self.controller.macro_owned = self.mapping.owned_swipes(self.profile)

    def save_store(self) -> None:
        if self.store is not None:
            try:
                self.store.save(self.mapping, self.profile)
            except OSError:
                _log.warning("could not save gesture macros", exc_info=True)

    def load_store(self) -> None:
        if self.store is None:
            return
        saved = self.store.load(self.mapping)
        if saved:
            try:
                self.apply_profile(saved, save=False)
            except ValueError:
                pass

    def check_foreground(self) -> None:
        """Called a few times a second from the glide thread: suggest (or, if auto_profile, apply) the app's profile."""
        if self.foreground_probe is None:
            return
        from app.gesture import profiles

        try:
            suggestion = profiles.suggest_for(self.foreground_probe()) or ""
        except Exception:
            return
        self.suggested = suggestion if suggestion != self.profile else ""
        if suggestion and self.settings.auto_profile and suggestion != self.profile:
            self.apply_profile(suggestion, save=False)

    def calibrate(self, top_left, bottom_right) -> bool:
        cal = Calibration.from_corners(tuple(top_left), tuple(bottom_right))
        if cal is None:
            return False
        self.settings.calibration = (cal.left, cal.top, cal.right, cal.bottom)
        self.settings.reach = "custom"
        self._apply_live()
        return True

    # ---- lifecycle -------------------------------------------------------------------------------------------------------------------------
    def enable(self, *, practice: bool = False) -> str:
        """Open the camera and start tracking. This is the ONLY way gesture control starts."""
        if self.running and self.practice != practice:
            self.disable("switching mode")          # practice <-> real: restart cleanly in the requested mode
        with self._lock:
            if self.running:
                return "Gesture control is already on."
            s = self.settings
            self.practice, self.problem = practice, ""
            device = sf.FakeInput() if practice else self.device_factory()
            cursor = VirtualCursor(self.screen_provider(s.monitor), CursorConfig(mirror=s.mirror, smoothing=s.smoothing), Calibration(*s.calibration))
            self.controller = sf.SafetyController(device, cursor, on_state=self._state_changed, right_click=s.right_click)
            self.controller.dispatch = self._dispatch
            self._sync_macros()
            self.controller.zoom_enabled, self.controller.swipe_mode = s.zoom, s.swipe
            self.controller.initializing()
            self._stop.clear()
            if not self._atexit:
                import atexit

                atexit.register(self.shutdown)       # never leave a mouse button pressed or the camera open when the app exits
                self._atexit = True
            self.stats = Stats(started=self.clock())
            self._thread = threading.Thread(target=self._run, name="gesture", daemon=True)
            self._thread.start()
            return "Gesture control is starting (practice mode: nothing is clicked)." if practice else "Gesture control is starting."

    def disable(self, reason: str = "turned off") -> str:
        with self._lock:
            if self.controller:
                self.controller.disable(reason)
            self._stop.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None
        return "Gesture control is off and the camera is released."

    def emergency_stop(self) -> None:
        """Independent of recognition: release buttons and disable immediately, then stop the worker."""
        if self.controller:
            self.controller.emergency_stop()
        self._stop.set()

    def shutdown(self) -> None:
        if self.controller:
            self.controller.shutdown()
        self.disable("shutdown")

    def pause(self) -> None:
        if self.controller:
            self.controller.pause("paused by you")

    def resume(self) -> bool:
        return bool(self.controller and self.controller.resume())

    def _state_changed(self, state: str, reason: str) -> None:
        if self.on_state:
            try:
                self.on_state(state, reason)
            except Exception:
                _log.debug("state listener failed", exc_info=True)

    # ---- worker ----------------------------------------------------------------------------------------------------------------------------
    def _make_detector(self):
        if self.detector_factory:
            return self.detector_factory(self.settings)
        raise RuntimeError("no hand detector is configured")

    def _limits(self) -> tuple[float, int]:
        """(frames/s cap, hands cap) from the performance mode; a failure means no cap."""
        if self.limits_provider is None:
            return 0.0, 2
        try:
            fps, hands = self.limits_provider()
            return (float(fps) if fps and fps < 30 else 0.0), max(1, int(hands))
        except Exception:
            return 0.0, 2

    def _run(self) -> None:
        from dataclasses import replace

        ctl, lease, detector = self.controller, None, None
        cap, hands_cap = self._limits()
        st = replace(self.settings, max_hands=min(self.settings.max_hands, hands_cap))
        self.fps_cap = cap
        last_taken = -1e9
        engine = MultiHandEngine(GestureConfig(min_confidence=st.min_confidence, drag_enabled=st.drag, fist_drag=st.fist_drag, double_click=st.double_click,
                                               right_click=st.right_click, mirror=st.mirror, zoom=st.zoom and st.max_hands >= 2, dwell_click=st.dwell_click))
        self._engine = engine
        try:
            detector = self._make_detector()
            detect_all = getattr(detector, "detect_all", None) or (lambda f, t: [h for h in [detector.detect(f, t)] if h is not None])
            lease = self.broker_factory().lease()
            ctl.ready()
            if not ctl.activate():
                raise RuntimeError("could not start control")
            threading.Thread(target=self._drive, name="gesture-glide", daemon=True).start()
            if self.practice:
                self.controller.pause("practice mode")   # practice never moves the mouse; it shows gestures
                self.controller._set(sf.PAUSED, "practice mode: nothing is clicked")
            while not self._stop.is_set():
                frame = lease.frame()
                if frame is None:
                    if not lease.healthy:
                        raise RuntimeError("the camera stopped")
                    self._stop.wait(0.005)
                    continue
                t0 = time.monotonic()
                if self.fps_cap and t0 - last_taken < 1.0 / self.fps_cap:
                    self._stop.wait(0.002)            # performance mode: leave the CPU to voice and typing
                    continue
                last_taken = t0
                hands = detect_all(frame, int(t0 * 1000))
                hand = hands[0] if hands else None
                self.stats.latencies_ms.append((time.monotonic() - t0) * 1000)
                self.stats.frames += 1
                self.stats.detected += bool(hands)
                result = engine.update(hands, self.clock())
                self.stats.last_gesture = result.gesture
                if self.practice:
                    self.preview_macros(result)
                    if self.on_frame:
                        self.on_frame(hand, result)
                    continue
                ctl.process(result)
                if self.on_frame:
                    self.on_frame(hand, result)
        except Exception as exc:
            self.problem = str(exc) or exc.__class__.__name__
            _log.warning("gesture control stopped", exc_info=True)
            ctl.fail(self.problem)
        finally:
            ctl.release_all()
            try:
                if detector is not None:
                    detector.close()
            finally:
                if lease is not None:
                    lease.close()
            if ctl.state not in (sf.ERROR,):
                ctl.disable("stopped")

    def _drive(self) -> None:
        """60 Hz pointer glide (separate from the camera rate)."""
        ticks = 0
        while not self._stop.wait(0.016):
            if self.controller:
                self.controller.tick()
            ticks += 1
            if ticks % 90 == 0:
                self.check_foreground()

    def _triggers(self, frame, now: float) -> list[tuple[str, float]]:
        """Macro triggers seen in this frame: (name, seconds held). A swipe is an event; FIST counts how long it has been held."""
        found = [(f"SWIPE_{e[1].upper()}", 0.0) for e in frame.events if e[0] == "swipe"]
        if frame.gesture == "FIST":
            self._fist_since = self._fist_since if self._fist_since is not None else now
            found.append(("FIST", now - self._fist_since))
        else:
            self._fist_since = None
        return found

    def _dispatch(self, frame) -> None:
        if not self.mapping.items:
            return
        now = self.clock()
        for trigger, held in self._triggers(frame, now):
            item = self.mapping.due(trigger, self.profile, held)
            if item is None:
                continue
            self.last_macro = f"{trigger} -> {item['tool']}"
            if self.run_action and not self.practice:
                self.run_action(item["tool"], item["args"], item["confirm"])

    def preview_macros(self, frame) -> None:
        """Practice mode: show what a gesture WOULD run, run nothing."""
        for trigger, held in self._triggers(frame, self.clock()):
            item = self.mapping.items.get(trigger)
            if item and item["enabled"] and item["profile"] in ("*", self.profile) and held >= item["hold_s"]:
                self.last_macro = f"(practice, not run) {trigger} -> {item['tool']}"

    # ---- reporting -------------------------------------------------------------------------------------------------------------------------
    def status(self) -> dict:
        broker = self.broker_factory() if self.running else None
        return {"state": self.state, "reason": self.controller.reason if self.controller else "disabled", "running": self.running,
                "practice": self.practice, "problem": self.problem, "profile": self.profile, "fps_cap": self.fps_cap or None,
                "suggested_profile": self.suggested, "last_macro": self.last_macro, "camera": broker.state if broker else cam.STOPPED,
                "holding_button": bool(self.controller and self.controller.holding), "stats": self.stats.summary(),
                "mappings": {g: {k: v for k, v in m.items() if k != "args"} for g, m in self.mapping.items.items()}}


def _screen(monitor: int = 0) -> tuple[int, int, int, int]:
    try:
        from app.tools import winapi

        mons = winapi.monitors()
        if mons:
            m = mons[min(max(0, monitor), len(mons) - 1)]
            a = m.full or m.area      # the whole monitor, so the taskbar is reachable
            return (int(a.left), int(a.top), int(a.right), int(a.bottom))
    except Exception:
        _log.debug("monitor lookup failed", exc_info=True)
    return DEFAULT_SCREEN


SERVICE = GestureService()
