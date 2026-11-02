"""Screen watch: change/text detection, adaptive backoff, cooldown, privacy block, limits, indicator, stop, emergency lock."""

import asyncio

import numpy as np
import pytest

from app.core import screen_watch as SW
from app.tools.base import ToolError


def img(v):
    return np.full((9, 16), v, dtype=np.uint8)


def obs(v, text=None, title="Doc", blocked=""):
    async def read():
        return text or ""
    return SW.Observation(thumb=None if blocked else img(v), title=title, read_text=read, blocked=blocked)


def watch(**kw):
    import time
    d = dict(id=1, target="Doc", condition="change", ends_at=time.monotonic() + 600)
    d.update(kw)
    return SW.Watch(**d)


def run(coro):
    return asyncio.run(coro)


def test_first_look_is_a_baseline_then_change_alerts_once():
    w = watch()
    assert run(SW.step(w, obs(10), 0)) is None                   # baseline, no alert
    assert run(SW.step(w, obs(10), 3)) is None and w.interval > w.base
    msg = run(SW.step(w, obs(200), 6))
    assert msg and "changed" in msg and w.state == "triggered" and w.interval == w.base


def test_backoff_grows_to_the_limit_and_resets_on_change():
    w = watch(repeat=True, cooldown=0)
    run(SW.step(w, obs(10), 0))
    for t in range(1, 15):
        run(SW.step(w, obs(10), t))
    assert w.interval == w.slowest
    run(SW.step(w, obs(250), 20))
    assert w.interval == w.base


def test_cooldown_suppresses_alert_flooding_when_repeating():
    w = watch(repeat=True, cooldown=60)
    run(SW.step(w, obs(10), 0))
    assert run(SW.step(w, obs(100), 10))                          # first alert
    assert run(SW.step(w, obs(200), 20)) is None                  # changed again but inside the cooldown
    assert run(SW.step(w, obs(10), 80))                           # after the cooldown


def test_text_watch_reads_only_after_a_change_and_alerts_on_match():
    w = watch(condition="text", phrase="Build finished")
    run(SW.step(w, obs(10, "compiling"), 0))
    assert w.ocr_runs == 1
    run(SW.step(w, obs(10, "compiling"), 3))
    run(SW.step(w, obs(10, "compiling"), 6))
    assert w.ocr_runs == 1                                        # unchanged picture: no OCR
    msg = run(SW.step(w, obs(90, "Build finished OK"), 9))
    assert msg and "Build finished" in msg and w.state == "triggered" and w.ocr_runs == 2


def test_blocked_window_captures_nothing_and_stops_the_watch():
    w = watch()
    assert run(SW.step(w, obs(0, blocked="KeePass is open"), 0)) is None
    assert w.state == "blocked" and "nothing was captured" in w.note


def test_time_limit_ends_the_watch():
    w = watch(ends_at=5)
    run(SW.step(w, obs(1), 10))
    assert w.state == "ended" and "time limit" in w.note


def test_manager_limits_validation_and_indicator():
    async def go():
        m = SW.WatchManager()
        lit = []
        m.indicator = lit.append

        async def observe(t):
            return obs(5)
        notes = []
        a = m.start(observe, lambda t, x: notes.append(x), base=1)
        assert lit == [True]
        for _ in range(SW.MAX_WATCHES - 1):
            m.start(observe, lambda t, x: None, base=1)
        with pytest.raises(ToolError):
            m.start(observe, lambda t, x: None)                   # fourth
        with pytest.raises(ToolError):
            m.start(observe, lambda t, x: None, condition="text", phrase="a")
        with pytest.raises(ToolError):
            m.start(observe, lambda t, x: None, minutes=SW.MAX_MINUTES + 1)
        m.pause(a.id, True)
        assert a.state == "paused"
        assert m.stop() == SW.MAX_WATCHES
        await asyncio.sleep(0.05)
        assert lit[-1] is False and m.active() == []
    run(go())


def test_alert_is_delivered_and_watch_ends(monkeypatch):
    async def go():
        m = SW.WatchManager()
        seq = iter([obs(10), obs(10), obs(220)])
        notes = []

        async def observe(t):
            return next(seq)
        w = m.start(observe, lambda title, msg: notes.append(msg), base=1)
        w.interval = 0.01
        w.base = 0.01
        for _ in range(100):
            await asyncio.sleep(0.02)
            if w.state != "running":
                break
        return w, notes
    w, notes = run(go())
    assert w.state == "triggered" and notes and "changed" in notes[0]


def test_emergency_lock_ends_every_watch():
    async def go():
        m = SW.WatchManager()

        async def observe(t):
            return obs(5)
        w = m.start(observe, lambda a, b: None, base=1, locked=lambda: True)
        await asyncio.sleep(0.05)
        return w
    w = run(go())
    assert w.state == "ended" and "emergency" in w.note


def test_a_failing_look_does_not_end_the_watch_silently():
    async def go():
        m = SW.WatchManager()
        calls = []

        async def observe(t):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return obs(5)
        w = m.start(observe, lambda a, b: None, base=1)
        w.interval = 0.01
        await asyncio.sleep(0.1)
        m.stop()
        return w, calls
    w, calls = run(go())
    assert len(calls) >= 2


def test_tool_list_stop_and_permissions():
    from app.config.settings import Settings
    from app.tools.base import ToolContext
    from app.tools.screen_watch_tool import WatchArgs, screen_watch, TOOLS

    ctx = ToolContext(Settings())
    assert "Nothing is being watched" in run(screen_watch(WatchArgs(), ctx)).message
    assert run(screen_watch(WatchArgs(action="stop_all"), ctx)).ok
    tool = TOOLS[0]
    assert int(tool.level_for(WatchArgs(action="start"), ctx)) > int(tool.level_for(WatchArgs(action="stop_all"), ctx))
    assert "never save pictures" in tool.confirmation_text(WatchArgs(action="start"), ctx)


# ---- "seeing my screen" means looking, not a screenshot file; assist mode --------------------------------------------------------------------
def test_seeing_phrases_look_at_the_screen_and_never_take_a_screenshot():
    from app.agent import shortcuts

    for text in ("what the fuck are you seeing my screen?", "I am asking you what are you seeing in my screen?", "what are you seeing in my screen",
                 "what do you see on my display", "what's happening on my screen", "tell me what you are looking at on my screen"):
        call = shortcuts.match_shortcut(text, lambda name: None)
        assert call is not None and call.name == "describe_screen", text
    shot = shortcuts.match_shortcut("take a screenshot", lambda name: None)
    assert shot is None or shot.name == "take_screenshot"      # never turned into a description


def test_assist_commands_start_and_stop_the_watch():
    from app.agent import shortcuts

    for text in ("watch my screen and help me", "monitor my screen", "keep an eye on my screen", "screen assist", "assist me on my screen"):
        call = shortcuts.match_shortcut(text, lambda name: None)
        assert call and call.name == "screen_watch" and call.arguments["condition"] == "assist", text
    off = shortcuts.match_shortcut("stop watching my screen", lambda name: None)
    assert off and off.arguments == {"action": "stop_all"}


def test_assist_waits_for_the_screen_to_settle_and_stays_quiet_when_nothing_is_useful():
    asyncio.run(_assist_flow())


async def _assist_flow():
    import time

    from app.core import screen_watch as sw

    tips = ["You have a failing test on line 12: the import is wrong.", "NOTHING"]

    async def describe():
        return tips.pop(0)

    w = sw.Watch(1, "", "assist", repeat=True, ends_at=time.monotonic() + 600, cooldown=0.0)
    import numpy as np

    img_a, img_b = np.zeros((8, 8), dtype="uint8"), np.full((8, 8), 255, dtype="uint8")
    obs = lambda img: sw.Observation(thumb=img, title="Editor", describe=describe)   # noqa: E731
    assert await sw.step(w, obs(img_a), 1.0) is None        # first look: baseline, pending
    assert await sw.step(w, obs(img_b), 2.0) is None        # changed again (still typing): no comment yet
    assert await sw.step(w, obs(img_b), 3.0) == "You have a failing test on line 12: the import is wrong."   # settled: speaks
    assert await sw.step(w, obs(img_b), 4.0) is None        # nothing new: quiet
    assert await sw.step(w, obs(img_a), 5.0) is None
    assert await sw.step(w, obs(img_a), 6.0) is None        # settled again but the model says NOTHING: stays silent
    assert w.alerts == 1


def test_assist_respects_the_cooldown_and_blocked_windows():
    import asyncio
    import time

    from app.core import screen_watch as sw

    import numpy as np

    calls = []

    async def describe():
        calls.append(1)
        return "Tip."

    w = sw.Watch(1, "", "assist", repeat=True, ends_at=time.monotonic() + 600, cooldown=45.0)
    img = np.zeros((8, 8), dtype="uint8")
    other = np.full((8, 8), 200, dtype="uint8")

    async def run():
        await sw.step(w, sw.Observation(thumb=img, describe=describe), 0.0)
        await sw.step(w, sw.Observation(thumb=img, describe=describe), 1.0)     # speaks
        await sw.step(w, sw.Observation(thumb=other, describe=describe), 2.0)
        await sw.step(w, sw.Observation(thumb=other, describe=describe), 3.0)   # settled but inside the cooldown: not asked
        blocked = await sw.step(w, sw.Observation(blocked="a password manager"), 4.0)
        return blocked

    assert asyncio.run(run()) is None
    assert len(calls) == 1 and w.state == "blocked"


def test_live_screen_phrases_start_assist_not_a_one_off_look():
    from app.agent import shortcuts

    for text in ("watch my screen live", "I want live screen monitoring", "monitor my screen in real time", "keep watching my screen"):
        call = shortcuts.match_shortcut(text, lambda name: None)
        assert call and call.name == "screen_watch" and call.arguments["condition"] == "assist", text


def test_screenshot_commands_save_a_file_and_monitor_zero_is_tolerated():
    from app.agent import shortcuts
    from app.tools.vision import ScreenArgs

    for text in ("take a screenshot", "take a screenshot please", "Take a screenshot of my screen", "capture my screen", "screenshot",
                 "save a screenshot", "take screenshot and save it to the downloads"):
        call = shortcuts.match_shortcut(text, lambda name: None)
        assert call and call.name == "take_screenshot", text
    assert ScreenArgs.model_validate({"target": "window", "window": "chrome", "monitor": 0}).monitor is None     # the failure in the chat
    assert ScreenArgs.model_validate({"target": "screen", "monitor": 2}).monitor == 2
