"""Gesture control: synthetic landmarks, a fake mouse, a fake camera/detector. Nothing here touches a real webcam or the OS; real-hardware
tracking is checked on the user's PC (`python -m app.main --setup-hand-model`, then the gesture_control tool's diagnostic)."""

from __future__ import annotations

import time

import pytest

from app.gesture import camera as cam
from app.gesture import engine as eng
from app.gesture import safety as sf
from app.gesture.cursor import Calibration, CursorConfig, VirtualCursor
from app.gesture.landmarks import Hand, extended_fingers, pinch_ratio, synthetic_hand
from app.gesture.service import GestureService

SCREEN = (0, 0, 1920, 1080)


def feed(engine, hands, step=0.033, start=0.0):
    out, t = [], start
    for h in hands:
        out.append(engine.update(h, t))
        t += step
    return out


# ---- landmarks / geometry ---------------------------------------------------------------------------------------------------------------------
def test_hand_validation():
    with pytest.raises(ValueError):
        Hand(((0, 0, 0),) * 20)
    with pytest.raises(ValueError):
        Hand(((float("nan"), 0, 0),) * 21)


def test_synthetic_poses_are_recognised():
    assert extended_fingers(synthetic_hand(extended=("index",))) == {"index"}
    assert extended_fingers(synthetic_hand(extended=("index", "middle"))) == {"index", "middle"}
    assert len(extended_fingers(synthetic_hand(extended=("index", "middle", "ring", "pinky")))) == 4
    assert extended_fingers(synthetic_hand(extended=())) == set()
    assert pinch_ratio(synthetic_hand(pinch=True)) < 0.28 < pinch_ratio(synthetic_hand())


def test_scale_invariant():
    near, far = synthetic_hand(pinch=True, size=0.3), synthetic_hand(pinch=True, size=0.1)
    assert abs(pinch_ratio(near) - pinch_ratio(far)) < 0.05


# ---- engine -----------------------------------------------------------------------------------------------------------------------------------
def test_index_point_needs_stable_frames():
    e = eng.GestureEngine()
    frames = feed(e, [synthetic_hand()] * 4)
    assert frames[-1].gesture == eng.INDEX_POINT and frames[-1].point is not None


def test_pinch_clicks_once_when_held_and_released():
    e = eng.GestureEngine()
    frames = feed(e, [synthetic_hand()] * 3 + [synthetic_hand(pinch=True)] * 10 + [synthetic_hand()] * 3)
    clicks = [ev for f in frames for ev in f.events if ev[0] == "click"]
    assert len(clicks) == 1
    states = [f.pinch for f in frames]
    assert states.index(eng.PINCH_STARTED) < states.index(eng.PINCH_HELD) < states.index(eng.PINCH_RELEASED)


def test_long_pinch_without_drag_is_not_a_click():
    e = eng.GestureEngine()
    frames = feed(e, [synthetic_hand(pinch=True)] * 40 + [synthetic_hand()] * 3, step=0.05)
    assert not [ev for f in frames for ev in f.events if ev[0] == "click"]


def test_pinch_hysteresis_does_not_flicker():
    e = eng.GestureEngine()
    feed(e, [synthetic_hand()] * 2 + [synthetic_hand(pinch=True)] * 3)
    mid = synthetic_hand(pinch=True)
    # a hand whose pinch ratio sits between pinch_on and pinch_off must stay pinched
    pts = list(mid.points)
    from app.gesture.landmarks import INDEX_TIP, THUMB_TIP, hand_size
    pts[THUMB_TIP] = (pts[INDEX_TIP][0] - hand_size(mid) * 0.35, pts[INDEX_TIP][1], 0.0)
    wobble = Hand(tuple(pts))
    assert 0.28 < pinch_ratio(wobble) < 0.42
    assert e.update(wobble, 1.0).pinch in (eng.PINCH_HELD, eng.PINCH_STARTED)


def test_drag_start_and_end_when_enabled():
    e = eng.GestureEngine(eng.GestureConfig(drag_enabled=True))
    frames = feed(e, [synthetic_hand()] * 2 + [synthetic_hand(pinch=True)] * 30 + [synthetic_hand()] * 3, step=0.05)
    kinds = [ev[0] for f in frames for ev in f.events]
    assert kinds.count("drag_start") == 1 and kinds.count("drag_end") == 1 and "click" not in kinds


def test_open_palm_hold_fires_once():
    e = eng.GestureEngine()
    palm = synthetic_hand(extended=("index", "middle", "ring", "pinky"))
    frames = feed(e, [palm] * 60, step=0.05)
    assert sum(1 for f in frames for ev in f.events if ev[0] == "palm_hold") == 1


def test_two_finger_scroll_direction():
    e = eng.GestureEngine()
    frames = feed(e, [synthetic_hand(extended=("index", "middle"), at=(0.5, 0.7 - i * 0.02)) for i in range(15)])
    total = sum(ev[1] for f in frames for ev in f.events if ev[0] == "scroll")
    assert total > 0
    e2 = eng.GestureEngine()
    down = feed(e2, [synthetic_hand(extended=("index", "middle"), at=(0.5, 0.3 + i * 0.02)) for i in range(15)])
    assert sum(ev[1] for f in down for ev in f.events if ev[0] == "scroll") < 0


def test_low_confidence_and_loss_release_drag():
    e = eng.GestureEngine(eng.GestureConfig(drag_enabled=True))
    feed(e, [synthetic_hand()] * 2 + [synthetic_hand(pinch=True)] * 20, step=0.05)
    assert e.dragging
    assert e.update(None, 1.0).events == []        # first miss: inside the loss timeout
    out = e.update(None, 1.5)
    assert ("lost",) in out.events and ("drag_end",) in out.events and not e.dragging


def test_disabled_gesture_is_ignored():
    e = eng.GestureEngine(eng.GestureConfig(enabled_gestures=frozenset({eng.INDEX_POINT})))
    frames = feed(e, [synthetic_hand(pinch=True)] * 5 + [synthetic_hand()] * 3)
    assert not [ev for f in frames for ev in f.events if ev[0] == "click"]


# ---- cursor -----------------------------------------------------------------------------------------------------------------------------------
def test_cursor_mirroring_margins_and_clamping():
    c = VirtualCursor(SCREEN, CursorConfig(mirror=True), Calibration())
    left_of_image = c.target(0.1, 0.5)
    right_of_image = c.target(0.9, 0.5)
    assert left_of_image[0] > right_of_image[0]              # mirrored
    assert c.target(0.0, 0.0) == (1919.0, 0.0)
    assert all(0 <= v <= 1919 for v in (c.target(5, 5)[0], c.target(-3, 0.5)[0]))


def test_cursor_dead_zone_smoothing_and_speed_limit():
    c = VirtualCursor(SCREEN, CursorConfig(mirror=False, smoothing=0.5, dead_zone_px=5, max_speed_px_s=1000, adaptive=False), Calibration(0, 0, 1, 1))
    first = c.move(0.5, 0.5, 0.0)
    assert first is not None
    assert c.move(0.5 + 1 / 1920, 0.5, 0.01) is None         # inside the dead zone
    jump = c.move(1.0, 0.5, 0.1)
    assert jump[0] - first[0] <= 1000 * 0.1 + 1               # speed capped


def test_calibration_validation():
    assert Calibration.from_corners((0.1, 0.1), (0.9, 0.9)) is not None
    assert Calibration.from_corners((0.5, 0.5), (0.55, 0.55)) is None


def test_multi_monitor_offset():
    c = VirtualCursor((-1920, 0, 0, 1080), CursorConfig(mirror=False), Calibration(0, 0, 1, 1))
    x, y = c.target(0.0, 0.0)
    assert x == -1920 and c.target(1.0, 1.0) == (-1.0, 1079.0)


# ---- safety controller ------------------------------------------------------------------------------------------------------------------------
def controller(**kw):
    dev = sf.FakeInput()
    cur = VirtualCursor(SCREEN, CursorConfig(mirror=False), Calibration(0, 0, 1, 1))
    ctl = sf.SafetyController(dev, cur, **kw)
    return ctl, dev


def active(ctl):
    ctl.initializing(), ctl.ready()
    assert ctl.activate()


def frame(gesture=eng.INDEX_POINT, pinch=eng.NOT_PINCHED, point=(0.5, 0.5), events=()):
    return eng.GestureFrame(gesture=gesture, pinch=pinch, point=point, events=list(events))


def test_disabled_by_default_and_ignores_input():
    ctl, dev = controller()
    assert ctl.state == sf.DISABLED
    ctl.process(frame(events=[("click",)]))
    assert dev.log == []


def test_active_click_and_move():
    ctl, dev = controller()
    active(ctl)
    ctl.process(frame(events=[("click",)]))
    ctl.tick()                                   # the 60 Hz glide places the pointer
    assert ("click", "left") in dev.log and any(i[0] == "move" for i in dev.log)


def test_paused_controller_sends_nothing():
    ctl, dev = controller()
    active(ctl)
    ctl.pause()
    ctl.process(frame(events=[("click",)]))
    assert dev.log == []


def test_emergency_stop_releases_held_button():
    ctl, dev = controller()
    active(ctl)
    ctl.process(frame(events=[("drag_start",)]))
    assert ctl.holding
    ctl.emergency_stop()
    assert ("up", "left") in dev.log and ctl.state == sf.DISABLED and not ctl.holding


def test_tracking_loss_releases_and_recovers():
    ctl, dev = controller()
    active(ctl)
    ctl.process(frame(events=[("drag_start",)]))
    ctl.process(frame(gesture=eng.NONE, point=None, events=[("lost",)]))
    assert ctl.state == sf.TRACKING_LOST and ("up", "left") in dev.log
    ctl.process(frame())
    assert ctl.state == sf.ACTIVE


def test_input_failure_fails_closed_and_releases():
    ctl, dev = controller()
    active(ctl)
    ctl.process(frame(events=[("drag_start",)]))
    dev.fail = True
    ctl.process(frame(events=[("click",)]))
    assert ctl.state == sf.ERROR and not ctl.holding


def test_shutdown_during_drag_releases():
    ctl, dev = controller()
    active(ctl)
    ctl.process(frame(events=[("drag_start",)]))
    ctl.shutdown()
    assert ("up", "left") in dev.log and ctl.state == sf.DISABLED


def test_only_buttons_we_pressed_are_released():
    ctl, dev = controller()
    active(ctl)
    ctl.release_all()
    assert not [i for i in dev.log if i[0] == "up"]


def test_palm_hold_pauses():
    ctl, _ = controller()
    active(ctl)
    ctl.process(frame(gesture=eng.OPEN_PALM, events=[("palm_hold",)]))
    assert ctl.state == sf.PAUSED and ctl.resume()


def test_scroll_is_capped():
    ctl, dev = controller()
    active(ctl)
    ctl.process(frame(events=[("scroll", 50)]))
    assert ("scroll", sf.MAX_SCROLL_PER_FRAME) in dev.log


def test_mapping_registry_rules():
    t = [0.0]
    m = sf.MappingRegistry({"open_app"}, clock=lambda: t[0])
    with pytest.raises(ValueError):
        m.add("FIST", "run_command")
    with pytest.raises(ValueError):
        m.add("FIST", "nonexistent")
    for mouse_gesture in ("PINCH", "INDEX_POINT", "OPEN_PALM", "TWO_FINGER_SCROLL"):       # these already move the mouse or pause
        with pytest.raises(ValueError):
            m.add(mouse_gesture, "open_app")
    m.add("FIST", "open_app", cooldown_s=2)
    assert m.due("FIST", held_s=0.1) is None             # a fist must be HELD (relaxing a hand must not fire it)
    assert m.due("FIST", held_s=1.0) and m.due("FIST", held_s=1.0) is None
    t[0] = 3.0
    assert m.due("FIST", held_s=1.0)
    m.items["FIST"]["enabled"] = False
    t[0] = 9.0
    assert m.due("FIST", held_s=1.0) is None


# ---- camera broker ----------------------------------------------------------------------------------------------------------------------------
class FakeCam:
    def __init__(self, frames=None):
        self.n, self.closed = 0, False
        self.frames = frames

    def frame(self):
        self.n += 1
        if self.frames is not None and self.n > len(self.frames):
            return None
        return self.n

    def close(self):
        self.closed = True


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_broker_opens_only_on_lease_and_closes_after_last():
    opened = []
    active_calls = []
    b = cam.CameraBroker(lambda: opened.append(FakeCam()) or opened[-1], fps=200, on_active=active_calls.append)
    assert b.state == cam.STOPPED and not opened
    a, c = b.lease(), b.lease()
    assert len(opened) == 1 and b.leases == 2
    assert wait_for(lambda: a.frame() is not None)
    assert wait_for(lambda: c.frame() is not None)        # both consumers see frames
    a.close()
    assert not opened[0].closed
    c.close()
    assert opened[0].closed and b.state == cam.STOPPED and active_calls == [True, False]


def test_broker_camera_unavailable():
    def boom():
        raise RuntimeError("busy")

    b = cam.CameraBroker(boom)
    with pytest.raises(RuntimeError):
        b.lease()
    assert b.state == cam.UNAVAILABLE and b.leases == 0


def test_broker_reports_lost_camera():
    b = cam.CameraBroker(lambda: FakeCam(frames=[1, 2]), fps=1000)
    lease = b.lease()
    assert wait_for(lambda: b.state == cam.UNAVAILABLE, 5.0)
    assert not lease.healthy
    lease.close()


def test_broker_keeps_only_newest_frame():
    b = cam.CameraBroker(lambda: FakeCam(), fps=500)
    lease = b.lease()
    time.sleep(0.1)
    got = lease.frame()
    assert got is not None and got > 1            # frames were overwritten, not queued
    assert b.captured > 1 and b._frame is not None
    lease.close()


# ---- service ----------------------------------------------------------------------------------------------------------------------------------
class ScriptDetector:
    def __init__(self, hands):
        self.hands, self.closed = list(hands), False

    def detect(self, frame, ts):
        return self.hands[min(frame - 1, len(self.hands) - 1)]

    def close(self):
        self.closed = True


def make_service(hands, *, fail_detector=False, camera=None):
    dev = sf.FakeInput()
    broker = cam.CameraBroker(lambda: camera or FakeCam(), fps=400)
    det = ScriptDetector(hands)

    def make_detector(_s):
        if fail_detector:
            raise RuntimeError("model missing")
        return det

    svc = GestureService(broker_factory=lambda: broker, detector_factory=make_detector, device_factory=lambda: dev,
                         screen_provider=lambda _m: SCREEN)
    return svc, dev, broker, det


def test_service_end_to_end_click_with_fake_hardware():
    hands = [synthetic_hand()] * 5 + [synthetic_hand(pinch=True)] * 5 + [synthetic_hand()] * 400
    svc, dev, broker, det = make_service(hands)
    svc.enable()
    assert wait_for(lambda: ("click", "left") in dev.log, 5.0)
    assert svc.status()["stats"]["frames_processed"] > 0
    svc.disable()
    assert det.closed and broker.state == cam.STOPPED and svc.state == sf.DISABLED


def test_service_practice_mode_never_sends_input():
    hands = [synthetic_hand()] * 5 + [synthetic_hand(pinch=True)] * 5 + [synthetic_hand()] * 400
    svc, dev, _, _ = make_service(hands)
    seen = []
    svc.on_frame = lambda h, f: seen.append(f.events)
    svc.enable(practice=True)
    assert wait_for(lambda: any(("click",) in e for e in seen), 5.0)
    assert dev.log == [] and svc.controller.device.log == []
    svc.disable()


def test_service_detector_failure_fails_closed_and_releases_camera():
    svc, _, broker, _ = make_service([synthetic_hand()], fail_detector=True)
    svc.enable()
    assert wait_for(lambda: svc.state == sf.ERROR)
    assert "model missing" in svc.problem and broker.state in (cam.STOPPED,) and broker.leases == 0


def test_service_camera_loss_stops_and_releases():
    svc, dev, broker, _ = make_service([synthetic_hand()] * 50, camera=FakeCam(frames=list(range(1, 4))))
    svc.enable()
    assert wait_for(lambda: svc.state == sf.ERROR, 6.0)
    assert broker.leases == 0 and not svc.controller.holding


def test_service_emergency_stop_independent_of_recognition():
    svc, dev, broker, det = make_service([synthetic_hand()] * 3 + [synthetic_hand(pinch=True)] * 1000)
    svc.configure(drag=True)
    svc.enable()
    assert wait_for(lambda: svc.controller.holding, 5.0)
    svc.emergency_stop()
    assert wait_for(lambda: broker.leases == 0 and not svc.running, 3.0)
    assert not svc.controller.holding and ("up", "left") in dev.log and svc.state == sf.DISABLED


def test_service_shutdown_during_drag():
    svc, dev, _, _ = make_service([synthetic_hand()] * 3 + [synthetic_hand(pinch=True)] * 1000)
    svc.configure(drag=True)
    svc.enable()
    assert wait_for(lambda: svc.controller.holding, 5.0)
    svc.shutdown()
    assert ("up", "left") in dev.log and not svc.controller.holding


def test_configure_validates():
    svc = GestureService()
    with pytest.raises(ValueError):
        svc.configure(nonsense=1)
    svc.configure(smoothing=5, max_hands=9, fps=1000, calibration=(0.9, 0.9, 0.1, 0.1))
    s = svc.settings
    assert s.smoothing <= 0.9 and s.max_hands == 2 and s.fps == 60 and Calibration(*s.calibration).valid()
    assert not svc.calibrate((0.5, 0.5), (0.52, 0.52))


def test_presence_coexists_with_gestures_via_one_device():
    opened = []
    b = cam.CameraBroker(lambda: opened.append(1) or FakeCam(), fps=300)
    from app.vision.presence import PresenceMonitor

    seen = []

    class Det:
        def has_face(self, f):
            seen.append(f)
            return True

    p = PresenceMonitor(away_minutes=5, on_away=lambda: None, on_return=lambda s: None, camera_factory=b.lease, detector_factory=Det)
    p.check_seconds = 0.01
    gesture_lease = b.lease()
    p.start()
    assert wait_for(lambda: len(seen) > 0, 3.0)
    p.stop()
    assert len(opened) == 1 and b.leases == 1          # the gesture lease still holds the device
    gesture_lease.close()
    assert b.state == cam.STOPPED


# ---- detector adapter / setup -----------------------------------------------------------------------------------------------------------------
def test_hand_from_result_and_availability(tmp_path):
    from types import SimpleNamespace as NS

    from app.gesture import detector as d

    lm = [NS(x=i / 21, y=0.5, z=0.0) for i in range(21)]
    res = NS(hand_landmarks=[lm], handedness=[[NS(category_name="Right", score=0.9)]])
    hand = d.hand_from_result(res)
    assert hand.handedness == "right" and hand.confidence == 0.9 and len(hand.points) == 21
    assert d.hand_from_result(NS(hand_landmarks=[], handedness=[])) is None
    ok, why = d.availability(tmp_path)
    assert not ok and "setup-hand-model" in why


def test_model_download_is_explicit_and_verified(tmp_path):
    from app.gesture import detector as d

    with pytest.raises(RuntimeError):
        d.download_model(tmp_path, opener=lambda url: b"tiny")
    assert not d.model_path(tmp_path).exists()
    path = d.download_model(tmp_path, opener=lambda url: b"x" * (d.MIN_MODEL_BYTES + 1))
    assert path.is_file() and not path.with_suffix(".part").exists()


def test_feature_is_off_by_default():
    from app.config.settings import Settings

    assert Settings().gesture_control is False


# ---- right click, voice shortcuts, dashboard model, calibration wizard, tutorial, validation ------------------------------------------------------
def test_right_click_is_off_by_default_and_edge_triggered():
    hands = [synthetic_hand()] * 3 + [synthetic_hand(middle_pinch=True, extended=("index", "middle"))] * 5 + [synthetic_hand()] * 3
    assert not [ev for f in feed(eng.GestureEngine(), hands) for ev in f.events if ev[0] == "right_click"]
    on = feed(eng.GestureEngine(eng.GestureConfig(right_click=True)), hands)
    assert [ev for f in on for ev in f.events].count(("right_click",)) == 1


def test_right_click_reaches_the_mouse_only_when_enabled():
    ctl, dev = controller(right_click=True)
    active(ctl)
    ctl.process(frame(events=[("right_click",)]))
    assert ("click", "right") in dev.log
    ctl2, dev2 = controller()
    active(ctl2)
    ctl2.process(frame(events=[("right_click",)]))
    assert ("click", "right") not in dev2.log


def test_left_pinch_never_triggers_right_click():
    hands = [synthetic_hand()] * 3 + [synthetic_hand(pinch=True)] * 5 + [synthetic_hand()] * 3
    frames = feed(eng.GestureEngine(eng.GestureConfig(right_click=True)), hands)
    assert not [ev for f in frames for ev in f.events if ev[0] == "right_click"]


def test_voice_shortcuts():
    from app.agent.shortcuts import match_developer as m

    cases = {"enable gesture control": "enable", "stop gestures": "disable", "pause hand tracking": "pause", "resume gestures": "resume",
             "practice gestures": "practice", "is gesture control on?": "status", "what are the gestures": "list_gestures"}
    for text, action in cases.items():
        call = m(text)
        assert call and call.name == "gesture_control" and call.arguments == {"action": action}, text
    assert m("open chrome") is None
    # real mishearings from the user's microphone
    for heard in ("Jaster, Control, On,", "J star, Control, On", "jester control on", "gestures control on", "gesture control on"):
        call = m(heard.lower().strip(" ,."))
        assert call and call.arguments == {"action": "enable"}, heard
    assert m("jaster control off").arguments == {"action": "disable"}
    for heard in ("gesture control on place", "gesture control on please", "detect my finger", "detect my fingers", "track my hand", "start finger tracking",
                  "use my finger", "finger detection", "detect my index finger now"):
        call = m(heard)
        assert call and call.arguments == {"action": "enable"}, heard
    assert m("stop detecting my finger").arguments == {"action": "disable"}
    assert m("is gesture control on").arguments == {"action": "status"}


def test_dashboard_panels_show_only_measured_values():
    from app.gesture import dashboard as d

    st = {"state": "DISABLED", "reason": "disabled", "running": False, "practice": False, "problem": "", "camera": "STOPPED",
          "holding_button": False, "stats": {"frames_processed": 0, "frames_with_hand": 0, "fps_measured": None, "detect_ms_median": None,
                                             "detect_ms_p95": None}, "mappings": {}}
    p = d.panels(st, ready=(False, "model missing"), allowed=False, settings={})
    assert list(p) == ["Camera", "Hand tracking", "Virtual mouse", "Mappings", "Performance", "Safety"]
    assert any("not measured yet" in line for line in p["Performance"])
    assert any("NOT ready" in line for line in p["Hand tracking"]) and any("OFF" in line for line in p["Safety"])
    assert any("No gesture runs" in line for line in p["Mappings"])


def test_skeleton_projection_is_mirrored_and_bounded():
    from app.gesture import dashboard as d

    pts, bones = d.skeleton(synthetic_hand(at=(0.2, 0.5)), 640, 480)
    assert len(pts) == 21 and len(bones) == len(d.CONNECTIONS)
    assert all(0 <= x < 640 and 0 <= y < 480 for x, y in pts)
    assert sum(x for x, _ in pts) / 21 > 320            # a hand on the left of the image shows on the right (mirror)
    assert d.skeleton(None, 640, 480) == ([], [])


def test_calibration_wizard_captures_two_held_corners():
    from app.gesture import dashboard as d

    w = d.CalibrationWizard(hold_s=1.0)
    t = 0.0
    for at in ((0.8, 0.2), (0.2, 0.8)):
        for _ in range(40):
            w.feed(synthetic_hand(at=at), t)
            t += 0.05
        if at == (0.8, 0.2):
            assert w.step == 1
    assert w.done and w.result and w.result.valid()
    assert w.result.left < w.result.right and w.result.top < w.result.bottom


def test_calibration_wizard_needs_stillness_and_rejects_tiny_areas():
    from app.gesture import dashboard as d

    w = d.CalibrationWizard(hold_s=1.0)
    for i in range(40):                                 # moving fingertip never completes the hold
        w.feed(synthetic_hand(at=(0.2 + (i % 2) * 0.2, 0.5)), i * 0.05)
    assert w.step == 0 and not w.corners
    w2 = d.CalibrationWizard(hold_s=0.5)
    t = 0.0
    for _ in range(30):
        w2.feed(synthetic_hand(at=(0.5, 0.5)), t)
        t += 0.05
    assert not w2.done and w2.step == 0 and "too close" in w2.message


def test_tutorial_advances_only_on_the_real_gesture():
    from app.gesture import dashboard as d

    tut = d.Tutorial()
    assert not tut.feed(eng.GestureFrame(events=[("click",)]))           # wrong order does nothing
    assert tut.feed(eng.GestureFrame(gesture="INDEX_POINT", point=(0.1, 0.5))) is False
    assert tut.feed(eng.GestureFrame(gesture="INDEX_POINT", point=(0.5, 0.5)))
    for ev in ("click", "scroll", "palm_hold", "swipe", "zoom"):
        assert tut.feed(eng.GestureFrame(events=[(ev, 1)]))
    assert tut.done and "Done" in tut.prompt


class FakeGestureEnv:
    def __init__(self, **kw):
        self.kw = kw

    def is_windows(self): return self.kw.get("windows", True)
    def python_version(self): return (3, 14)
    def mediapipe(self): return self.kw.get("mp", (True, "0.10.14"))
    def model_ready(self): return self.kw.get("model", (True, "m.task"))
    def camera_frames(self, s): return self.kw.get("frames", (60, ""))
    def detect(self, s):
        from app.validation.gesture_checks import DetectRun
        return self.kw.get("run", DetectRun(120, 110, 4.0, [20.0] * 120))
    def dpi_awareness(self): return self.kw.get("dpi", "per-monitor")
    def screens(self): return [(0, 0, 1920, 1080), (-1920, 0, 0, 1080)]
    def pointer_roundtrip(self, x, y): return self.kw.get("round", lambda x, y: (x, y))(x, y)
    def taskbar_reachable(self): return self.kw.get("taskbar", True)


def test_validation_all_good_still_leaves_real_click_pending():
    from app.validation import gesture_checks as gc

    rep = gc.run(FakeGestureEnv())
    statuses = {r.name: r.status for r in rep.results}
    assert statuses["real hand detection"] == "pass" and statuses["real click lands on the right control"] == "pending"
    assert rep.verdict == "INCOMPLETE"                  # pending is never a pass


def test_validation_flags_real_problems():
    from app.validation import gesture_checks as gc
    from app.validation.gesture_checks import DetectRun

    assert gc.run(FakeGestureEnv(windows=False)).verdict == "INCOMPLETE"
    rep = gc.run(FakeGestureEnv(mp=(False, "no matching wheel"), dpi="unaware", round=lambda x, y: (x + 5, y)))
    by = {r.name: r for r in rep.results}
    assert by["mediapipe on this Python"].status == "fail" and "wheel" in by["mediapipe on this Python"].detail
    assert by["DPI awareness"].status == "fail"
    assert by[next(n for n in by if n.startswith("pointer"))].status == "fail"
    assert by["real hand detection"].status == "skip" and rep.verdict == "FAILED"
    assert gc.check_detection(DetectRun(100, 100, 4.0, [200.0] * 100))[0] == "fail"
    assert gc.check_detection(DetectRun(100, 10, 4.0, [10.0] * 100))[0] == "warn"
    assert gc.check_detection(DetectRun(100, 100, 4.0, [70.0] * 100))[0] == "warn"
    assert gc.check_detection(DetectRun(0, 0, 4.0, []))[0] == "fail"
    assert gc.run(FakeGestureEnv(frames=(0, "busy"))).results[-2].status == "skip"


def test_monitor_full_rect_is_used_by_the_gesture_cursor(monkeypatch):
    from app.gesture import service as svc
    from app.tools import winapi

    mons = [winapi.Monitor(winapi.Rect(0, 0, 1920, 1040), True, winapi.Rect(0, 0, 1920, 1080))]
    monkeypatch.setattr(winapi, "monitors", lambda: mons)
    assert svc._screen(0) == (0, 0, 1920, 1080)


def test_gesture_page_and_rail_entry_are_wired_without_qt():
    """The Qt page can't be imported in CI without a display; check it compiles and is registered (visual behaviour is checked by hand)."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "ui" / "command_center"
    ast.parse((root / "gesture_page.py").read_text(encoding="utf-8"))
    assert '"gestures"' in (root / "widgets.py").read_text(encoding="utf-8")
    assert "GesturePage" in (root / "view.py").read_text(encoding="utf-8")


# ---- smoothness and click accuracy ------------------------------------------------------------------------------------------------------------
def test_one_euro_steadies_jitter_but_follows_real_movement():
    import random

    from app.gesture.cursor import OneEuro

    rnd = random.Random(1)
    f = OneEuro()
    rest = [f(0.5 + rnd.uniform(-0.004, 0.004), i / 12) for i in range(60)]      # a resting hand at 12 fps with landmark jitter
    assert max(rest[20:]) - min(rest[20:]) < 0.0045                             # steadier than the raw +-0.004 jitter
    g = OneEuro()
    out = [g(i * 0.05, i / 12) for i in range(30)]                               # a hand sweeping at 0.6 / s
    assert abs(out[-1] - 29 * 0.05) < 0.08                                       # little lag when moving


def test_glide_is_smooth_between_slow_camera_frames():
    c = VirtualCursor(SCREEN, CursorConfig(mirror=False, smoothing=0.35), Calibration(0, 0, 1, 1))
    c.aim(0.2, 0.5, 0.0)
    assert c.glide(0.0) is not None                   # first call places the pointer
    c.aim(0.8, 0.5, 0.1)                              # the camera reports a jump 10x per second ...
    xs, t = [], 0.1
    for _ in range(6):                                # ... the pointer glides at 60 Hz instead of jumping
        t += 0.016
        p = c.glide(t)
        if p:
            xs.append(p[0])
    assert len(xs) >= 4 and xs == sorted(xs) and xs[0] < 0.8 * 1919 and len(set(xs)) == len(xs)
    assert all(b - a < 900 for a, b in zip(xs, xs[1:]))


def test_pinch_lands_where_you_aimed_not_where_the_fingers_drifted():
    ctl, dev = controller()
    active(ctl)
    t = [0.0]
    ctl.clock = lambda: t[0]
    for i in range(10):                                   # aiming at x = 0.30 for a second
        t[0] = i * 0.1
        ctl.process(frame(point=(0.30, 0.5)))
    t[0] = 1.0
    ctl.process(frame(gesture=eng.PINCH, pinch=eng.PINCH_STARTED, point=(0.55, 0.52)))   # fingers drift while closing
    moves = [i for i in dev.log if i[0] == "move"]
    assert abs(moves[-1][1] - 0.30 * 1919) < 60            # the click spot is the aimed one, not the drifted one
    for k in range(5):                                      # the pointer stays frozen while pinched
        t[0] = 1.1 + k * 0.05
        ctl.process(frame(gesture=eng.PINCH, pinch=eng.PINCH_HELD, point=(0.9, 0.9)))
        ctl.tick()
    assert all(abs(i[1] - 0.30 * 1919) < 60 for i in dev.log if i[0] == "move")


def test_settings_apply_live_and_presets():
    svc, dev, broker, _ = make_service([synthetic_hand()] * 2000)
    svc.enable()
    assert wait_for(lambda: svc.controller is not None and svc.state == sf.ACTIVE, 5.0)
    svc.configure(reach="large", smooth="smoother")
    cal = svc.controller.cursor.cal
    assert (cal.left, cal.right) == (0.1, 0.9) and svc.controller.cursor.cfg.smoothing == 0.6
    assert svc.calibrate((0.3, 0.3), (0.7, 0.7)) and svc.settings.reach == "custom"
    assert svc.controller.cursor.cal.left == 0.3
    svc.disable()


def test_tool_settings_validate_presets():
    from app.tools.gesture_tool import _parse
    from app.tools.base import ToolError

    assert _parse("reach", "Large") == "large" and _parse("smooth", "quicker") == "quicker"
    with pytest.raises(ToolError):
        _parse("reach", "huge")


def test_resume_and_pause_when_there_is_nothing_to_do_are_not_failures():
    import asyncio
    from types import SimpleNamespace

    from app.tools.gesture_tool import GestureArgs, gesture_control

    ctx = SimpleNamespace(settings=SimpleNamespace(gesture_control=True), paths=SimpleNamespace(models_dir=None), executor=None, apply_setting=None)
    for action in ("resume", "pause"):
        result = asyncio.run(gesture_control(GestureArgs(action=action), ctx))
        assert result.ok and "off" in result.message.lower()


def test_spoken_confirmation_is_short_and_understands_more_ways_to_say_yes():
    from app.agent.language import classify_answer

    for yes in ("yes", "confirm", "Yes, confirm", "proceed", "go ahead", "yes please", "turn it on", "do it"):
        assert classify_answer(yes) is True, yes
    for no in ("no", "cancel", "not now"):
        assert classify_answer(no) is False, no
    assert classify_answer("say yes or no") is None and classify_answer("lock my hand") is None     # its own question / noise is not an answer
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "controller_parts" / "voice.py").read_text(encoding="utf-8")
    ns: dict = {}
    exec(compile(ast.Module([n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_short_question"], []), "voice", "exec"), ns)
    from app.tools.gesture_tool import GestureArgs, _question

    spoken = ns["_short_question"](_question(GestureArgs(action="enable"), None))
    assert len(spoken) <= 115 and spoken.endswith(("?", "..."))


# ---- two hands, zoom, swipe, dwell, Midas-touch guard -----------------------------------------------------------------------------------------
def two(a_at, b_at, *, pinch=True, size=0.15):
    return [synthetic_hand(at=a_at, pinch=pinch, size=size), synthetic_hand(at=b_at, pinch=pinch, size=size)]


def run_multi(sequence, step=0.05, cfg=None):
    e = eng.MultiHandEngine(cfg or eng.GestureConfig())
    return e, [e.update(hands, i * step) for i, hands in enumerate(sequence)]


def zooms(frames):
    return [ev[1] for f in frames for ev in f.events if ev[0] == "zoom"]


def test_two_hands_pinching_apart_zoom_in_and_together_zoom_out():
    open_ = two((0.3, 0.5), (0.7, 0.5), pinch=False)
    apart = [two((0.3 - i * 0.02, 0.5), (0.7 + i * 0.02, 0.5)) for i in range(10)]
    _, frames = run_multi([open_] * 3 + apart)
    z = zooms(frames)
    assert z and sum(z) > 0 and all(0 < n <= 3 for n in z)
    together = [two((0.2 + i * 0.03, 0.5), (0.8 - i * 0.03, 0.5)) for i in range(10)]
    _, frames = run_multi([open_] * 3 + together)
    assert sum(zooms(frames)) < 0


def test_zoom_does_not_click_or_move_the_pointer_and_one_hand_never_zooms():
    open_ = two((0.3, 0.5), (0.7, 0.5), pinch=False)
    seq = [open_] * 3 + [two((0.3 - i * 0.02, 0.5), (0.7 + i * 0.02, 0.5)) for i in range(8)] + [open_] * 3
    _, frames = run_multi(seq)
    assert not [ev for f in frames for ev in f.events if ev[0] in ("click", "drag_start")]
    assert all(f.point is None for f in frames[5:11])
    one = [[synthetic_hand(at=(0.4, 0.5), pinch=True)] for _ in range(12)]
    assert not zooms(run_multi([[synthetic_hand()]] * 3 + one)[1])
    _, off = run_multi([open_] * 3 + [two((0.3 - i * 0.02, 0.5), (0.7 + i * 0.02, 0.5)) for i in range(8)], cfg=eng.GestureConfig(zoom=False))
    assert not zooms(off)


def test_a_second_hand_cannot_steal_the_pointer():
    pointing = synthetic_hand(at=(0.3, 0.5))
    e, frames = run_multi([[pointing]] * 4)
    start = frames[-1].point
    other = synthetic_hand(at=(0.75, 0.5), extended=("index",))
    frames = [e.update([other, pointing], 1.0 + i * 0.05) for i in range(4)]       # the other hand is listed first and also points
    assert all(abs(f.point[0] - start[0]) < 0.05 for f in frames if f.point)
    assert all(len(f.hands) == 2 for f in frames)


def test_open_palm_swipe_left_and_right_as_the_user_sees_it():
    palm = lambda x, y=0.5: synthetic_hand(at=(x, y), extended=("index", "middle", "ring", "pinky"))     # noqa: E731
    still = [[palm(0.7)]] * 4
    left_to_right = [[palm(0.7 - i * 0.07)] for i in range(8)]       # image x decreases = the user's hand moves to the RIGHT (mirrored view)
    _, frames = run_multi(still + left_to_right, step=0.06)
    swipes = [ev for f in frames for ev in f.events if ev[0] == "swipe"]
    assert swipes == [("swipe", "right")]
    right_to_left = [[palm(0.3 + i * 0.07)] for i in range(8)]
    _, frames = run_multi([[palm(0.3)]] * 4 + right_to_left, step=0.06)
    assert [ev for f in frames for ev in f.events if ev[0] == "swipe"] == [("swipe", "left")]


def test_swipe_needs_a_sideways_sweep_not_a_hold_or_a_diagonal():
    palm = lambda x, y: synthetic_hand(at=(x, y), extended=("index", "middle", "ring", "pinky"))          # noqa: E731
    _, held = run_multi([[palm(0.5, 0.5)]] * 30, step=0.06)
    assert not [ev for f in held for ev in f.events if ev[0] == "swipe"]
    assert sum(ev[0] == "palm_hold" for f in held for ev in f.events) == 1          # a still palm still pauses
    diagonal = [[palm(0.7 - i * 0.05, 0.3 + i * 0.05)] for i in range(10)]
    assert not [ev for f in run_multi(diagonal, step=0.06)[1] for ev in f.events if ev[0] == "swipe"]
    sweep = [[palm(0.8 - i * 0.07, 0.5)] for i in range(9)]
    _, frames = run_multi(sweep + sweep[::-1], step=0.06)
    assert sum(ev[0] == "swipe" for f in frames for ev in f.events) == 1             # the cooldown stops an immediate return swipe
    assert not [ev for f in frames for ev in f.events if ev[0] == "palm_hold"]       # a swipe never also pauses


def test_dwell_click_is_opt_in_and_clicks_once_per_stay():
    still = [[synthetic_hand(at=(0.5, 0.5))]] * 40
    _, off = run_multi(still, step=0.05)
    assert not [ev for f in off for ev in f.events if ev[0] == "click"]
    _, on = run_multi(still, step=0.05, cfg=eng.GestureConfig(dwell_click=True, dwell_s=1.0))
    assert sum(ev[0] == "click" for f in on for ev in f.events) == 1
    assert max(f.dwell for f in on) == 1.0
    moving = [[synthetic_hand(at=(0.3 + i * 0.01, 0.5))] for i in range(40)]
    assert not [ev for f in run_multi(moving, step=0.05, cfg=eng.GestureConfig(dwell_click=True, dwell_s=1.0))[1] for ev in f.events if ev[0] == "click"]


def test_a_hand_that_appears_already_pinched_must_open_before_it_can_click():
    seq = [[synthetic_hand(pinch=True)]] * 6 + [[synthetic_hand()]] * 3 + [[synthetic_hand(pinch=True)]] * 4 + [[synthetic_hand()]] * 3
    _, frames = run_multi(seq)
    assert sum(ev[0] == "click" for f in frames for ev in f.events) == 1             # only the second, deliberate pinch
    assert all(f.pinch == eng.NOT_PINCHED for f in frames[:6])


def test_safety_maps_zoom_and_swipe_and_only_releases_buttons_it_pressed():
    ctl, dev = controller()
    active(ctl)
    ctl.process(frame(events=[("zoom", 2), ("zoom", 9), ("zoom", -9)]))
    assert [i for i in dev.log if i[0] == "zoom"] == [("zoom", 2), ("zoom", 3), ("zoom", -3)]
    ctl.zoom_enabled = False
    ctl.process(frame(events=[("zoom", 1)]))
    assert len([i for i in dev.log if i[0] == "zoom"]) == 3
    ctl.process(frame(events=[("swipe", "left")]))
    assert not [i for i in dev.log if i[0] == "hotkey"]                              # swipe is off by default
    for mode, left, right in (("browser", "alt+left", "alt+right"), ("desktops", "ctrl+win+left", "ctrl+win+right"), ("slides", "left", "right")):
        dev.log.clear()
        ctl.swipe_mode = mode
        ctl.process(frame(events=[("swipe", "left"), ("swipe", "right")]))
        assert [i[1] for i in dev.log if i[0] == "hotkey"] == [left, right]
    dev.log.clear()
    ctl.process(frame(events=[("drag_end",)]))                                       # nothing was pressed by us: nothing is released
    assert not [i for i in dev.log if i[0] == "up"]


def test_service_runs_two_hands_end_to_end_with_detect_all():
    open_ = two((0.3, 0.5), (0.7, 0.5), pinch=False)
    script = [open_] * 4 + [two((0.3 - i * 0.015, 0.5), (0.7 + i * 0.015, 0.5)) for i in range(12)] + [open_] * 400
    dev = sf.FakeInput()
    broker = cam.CameraBroker(lambda: FakeCam(), fps=400)

    class TwoHands:
        def detect_all(self, frame, ts):
            return script[min(frame - 1, len(script) - 1)]

        def close(self):
            pass

    svc = GestureService(broker_factory=lambda: broker, detector_factory=lambda s: TwoHands(), device_factory=lambda: dev, screen_provider=lambda m: SCREEN)
    svc.enable()
    assert wait_for(lambda: any(i[0] == "zoom" for i in dev.log), 6.0)
    svc.configure(swipe="browser", zoom=False)
    assert svc.controller.swipe_mode == "browser" and svc.controller.zoom_enabled is False and svc._engine.cfg.zoom is False
    svc.disable()


def test_hands_from_result_returns_both_hands():
    from types import SimpleNamespace as NS

    from app.gesture import detector as d

    def lm(shift):
        return [NS(x=shift + i / 100, y=0.5, z=0.0) for i in range(21)]

    res = NS(hand_landmarks=[lm(0.1), lm(0.6)], handedness=[[NS(category_name="Left", score=0.9)], [NS(category_name="Right", score=0.8)]])
    hands = d.hands_from_result(res)
    assert len(hands) == 2 and hands[0].handedness == "left" and hands[1].handedness == "right"
    assert d.hands_from_result(NS(hand_landmarks=[], handedness=[])) == []


def test_tool_accepts_the_new_settings():
    from app.tools.base import ToolError
    from app.tools.gesture_tool import _parse

    assert _parse("swipe", "Browser") == "browser" and _parse("zoom", "off") is False and _parse("dwell_click", "on") is True
    with pytest.raises(ToolError):
        _parse("swipe", "sideways")
