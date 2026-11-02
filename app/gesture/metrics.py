"""Repeatable gesture benchmark: labelled scenarios -> engine -> safety controller -> a fake mouse, on a virtual clock.

Measures what a change can break: did the intended action happen (recognition), did anything happen that was NOT intended (false
activations), did an intended action NOT happen (missed), how far from the aim point did a click land (cursor error), and how long
the pipeline took per frame (latency, CPU time of this code only - not the camera or the detector).

Honest limits: the hands are SYNTHETIC (landmarks generated from a geometric model, with seeded jitter). This proves the logic does
not flicker, mis-click or hold a button on clean and noisy input and lets two versions be compared; it says nothing about how
MediaPipe sees a real hand. Real-hand accuracy comes from ``--validate gesture`` on the PC. Deterministic: same seed, same numbers.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.gesture import engine as eng
from app.gesture import safety as sf
from app.gesture.cursor import Calibration, CursorConfig, VirtualCursor
from app.gesture.landmarks import Hand, synthetic_hand

FRAME_S = 1 / 30
SCREEN = (0, 0, 1920, 1080)


@dataclass
class Scenario:
    name: str
    frames: list                         # per frame: a Hand, a list of Hands, or None (nothing visible)
    expect: dict = field(default_factory=dict)      # device action -> exact count, e.g. {"click": 1}; anything unlisted must be 0
    note: str = ""


@dataclass
class Outcome:
    name: str
    passed: bool
    got: dict
    expected: dict
    false_activations: int
    missed: int
    cursor_error_px: float | None
    left_button_down_at_end: bool
    ms_per_frame: float


def _jitter(hand: Hand, rng: random.Random, amount: float) -> Hand:
    if amount <= 0:
        return hand
    dx, dy = rng.uniform(-amount, amount), rng.uniform(-amount, amount)
    return Hand(tuple((x + dx + rng.uniform(-amount, amount) * 0.3, y + dy + rng.uniform(-amount, amount) * 0.3, z) for x, y, z in hand.points),
                hand.confidence, hand.handedness)


def standard_scenarios(seed: int = 7, noise: float = 0.004) -> list[Scenario]:
    """A fixed set that mirrors real use. ``noise`` is hand-tremor/detector jitter in normalised image units."""
    rng = random.Random(seed)
    j = lambda h: _jitter(h, rng, noise)                                     # noqa: E731
    point, pinch = synthetic_hand(), synthetic_hand(pinch=True)
    palm = synthetic_hand(extended=("index", "middle", "ring", "pinky"))
    fist = synthetic_hand(extended=())
    two = synthetic_hand(extended=("index", "middle"))
    idle = [j(point) for _ in range(60)]
    return [
        Scenario("point and hold still", [j(point) for _ in range(90)], {}, "must never click"),
        Scenario("one deliberate pinch", [j(point) for _ in range(10)] + [j(pinch) for _ in range(6)] + [j(point) for _ in range(20)],
                 {"click": 1}),
        Scenario("two separate pinches", [j(point) for _ in range(10)] + [j(pinch) for _ in range(6)] + [j(point) for _ in range(15)]
                 + [j(pinch) for _ in range(6)] + [j(point) for _ in range(20)], {"click": 2}),
        Scenario("one-frame pinch flicker", [j(point) for _ in range(15)] + [j(pinch)] + [j(point) for _ in range(30)], {},
                 "a single noisy frame is not a click"),
        Scenario("pinch-flicker burst", [j(point) for _ in range(15)] + [j(pinch), j(point), j(pinch), j(point)] + [j(point) for _ in range(30)],
                 {}, "chatter around the threshold is not a click"),
        Scenario("hand enters already pinched", [j(pinch) for _ in range(10)] + [j(point) for _ in range(20)], {},
                 "Midas guard: bringing a pinched hand into view is not a click"),
        Scenario("hand leaves mid-pinch", [j(point) for _ in range(10)] + [j(pinch) for _ in range(3)] + [None] * 20, {"click": 0},
                 "no button may stay down; the controller must release"),
        Scenario("two-finger scroll", [j(two) for _ in range(6)] + [j(synthetic_hand(extended=("index", "middle"), at=(0.5, 0.5 - 0.03 * i)))
                                                                      for i in range(1, 12)] + [j(two) for _ in range(5)], {"scroll": "any"},
                 "moving two fingers up scrolls"),
        Scenario("relaxed fist never clicks", idle + [j(fist) for _ in range(12)] + idle, {}, "a fist is not a pinch"),
        Scenario("short palm show does not pause", [j(point) for _ in range(10)] + [j(palm) for _ in range(5)] + [j(point) for _ in range(30)], {},
                 "pausing needs a held palm"),
        Scenario("no hand at all", [None] * 90, {}),
    ]


def _count(log: list) -> dict:
    out: dict = {}
    for item in log:
        out[item[0]] = out.get(item[0], 0) + 1
    return out


def run_scenario(sc: Scenario, *, calibration: tuple = (0.2, 0.2, 0.8, 0.8), config: dict | None = None) -> Outcome:
    t = [0.0]
    clock = lambda: t[0]                                                      # noqa: E731
    device = sf.FakeInput()
    cursor = VirtualCursor(SCREEN, CursorConfig(mirror=False), Calibration(*calibration))
    ctl = sf.SafetyController(device, cursor, clock=clock)
    ctl.initializing(), ctl.ready(), ctl.activate()
    engine = eng.MultiHandEngine(eng.GestureConfig(mirror=False, **(config or {})))
    elapsed = 0.0
    for item in sc.frames:
        hands = [] if item is None else (item if isinstance(item, list) else [item])
        t0 = time.perf_counter()
        frame = engine.update(hands, t[0])
        ctl.process(frame)
        ctl.tick()
        elapsed += time.perf_counter() - t0
        t[0] += FRAME_S
    ctl.release_all()
    got = _count([e for e in device.log if e[0] in ("click", "double", "scroll", "zoom", "hotkey", "down")])
    clicks = got.get("click", 0)
    want = dict(sc.expect)
    ok = True
    false_act = missed = 0
    for key in set(got) | set(want):
        have, need = got.get(key, 0), want.get(key, 0)
        if need == "any":
            if have == 0:
                ok, missed = False, missed + 1
        elif have > need:
            ok, false_act = False, false_act + (have - need)
        elif have < need:
            ok, missed = False, missed + (need - have)
    down = any(e[0] == "down" for e in device.log) and not any(e[0] == "up" for e in device.log[-1:])
    err = None
    if clicks and device.log:
        aim = cursor.pos
        last_move = next((e for e in reversed(device.log) if e[0] == "move"), None)
        if aim and last_move:
            err = round(((aim[0] - last_move[1]) ** 2 + (aim[1] - last_move[2]) ** 2) ** 0.5, 1)
    return Outcome(sc.name, ok and not down, got, want, false_act, missed, err, down, round(1000 * elapsed / max(1, len(sc.frames)), 4))


def run_suite(seed: int = 7, noise: float = 0.004, config: dict | None = None) -> dict:
    """All standard scenarios at one noise level -> a summary dict (JSON-safe)."""
    outcomes = [run_scenario(s, config=config) for s in standard_scenarios(seed, noise)]
    n = len(outcomes)
    lat = sorted(o.ms_per_frame for o in outcomes)
    return {
        "seed": seed, "noise": noise, "scenarios": n,
        "passed": sum(o.passed for o in outcomes),
        "recognition_accuracy": round(sum(o.passed for o in outcomes) / n, 3),
        "false_activations": sum(o.false_activations for o in outcomes),
        "missed": sum(o.missed for o in outcomes),
        "stuck_buttons": sum(o.left_button_down_at_end for o in outcomes),
        "ms_per_frame_median": lat[n // 2], "ms_per_frame_max": lat[-1],
        "failures": [asdict(o) for o in outcomes if not o.passed],
        "synthetic": True,
    }


def sweep(seed: int = 7, levels: tuple = (0.0, 0.004, 0.01, 0.02), config: dict | None = None) -> list[dict]:
    """The same suite at rising jitter: how gracefully does it degrade (false activations should stay at 0)."""
    return [run_suite(seed, n, config) for n in levels]


def compare(baseline: dict, now: dict) -> list[str]:
    """Regressions of ``now`` against a saved ``baseline`` (empty list = nothing got worse)."""
    problems = []
    if now["false_activations"] > baseline["false_activations"]:
        problems.append(f"false activations rose {baseline['false_activations']} -> {now['false_activations']}")
    if now["missed"] > baseline["missed"]:
        problems.append(f"missed gestures rose {baseline['missed']} -> {now['missed']}")
    if now["stuck_buttons"] > baseline["stuck_buttons"]:
        problems.append("a mouse button was left pressed")
    if now["recognition_accuracy"] < baseline["recognition_accuracy"]:
        problems.append(f"accuracy fell {baseline['recognition_accuracy']} -> {now['recognition_accuracy']}")
    if now["ms_per_frame_median"] > max(0.5, baseline["ms_per_frame_median"] * 3):
        problems.append(f"processing time per frame tripled ({baseline['ms_per_frame_median']} -> {now['ms_per_frame_median']} ms)")
    return problems


def save(path: Path, result: dict) -> None:
    Path(path).write_text(json.dumps(result, indent=1), encoding="utf-8")


def load(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
