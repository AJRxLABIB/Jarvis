"""Gesture profiles, persisted macros, the repeatable benchmark, and the run_action thread bridge."""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.gesture import engine as eng
from app.gesture import macros, metrics, profiles
from app.gesture import safety as sf
from app.gesture.service import GestureService
from tests.test_gesture import SCREEN
from app.gesture.landmarks import synthetic_hand


# ---------------------------------------------------------------- profiles
def test_every_profile_is_complete_and_never_binds_a_tool():
    for name in profiles.PROFILES:
        s = profiles.settings_for(name)
        assert s["reach"] in ("small", "medium", "large") and s["swipe"] in ("off", "browser", "desktops", "slides")
        assert not any(k in s for k in ("tool", "mapping", "macros")), name          # input behaviour only
    assert profiles.settings_for("general")["swipe"] == "off" and not profiles.settings_for("general")["right_click"]
    with pytest.raises(ValueError):
        profiles.settings_for("nope")


def test_apps_suggest_profiles():
    assert profiles.suggest_for("chrome.exe") == "browsing"
    assert profiles.suggest_for("POWERPNT.EXE") == "presentation"
    assert profiles.suggest_for("vlc.exe") == "media"
    assert profiles.suggest_for("notepad.exe") is None and profiles.suggest_for("") is None


def test_apply_profile_changes_live_settings_and_keeps_a_custom_calibration(tmp_path):
    svc = GestureService(screen_provider=lambda _m: SCREEN)
    svc.apply_profile("presentation", save=False)
    assert svc.settings.swipe == "slides" and svc.settings.reach == "large" and not svc.settings.zoom and svc.profile == "presentation"
    svc.settings.reach = "custom"
    svc.settings.calibration = (0.1, 0.1, 0.9, 0.9)
    svc.apply_profile("precision", save=False)
    assert svc.settings.calibration == (0.1, 0.1, 0.9, 0.9) and svc.settings.drag      # hand-made calibration survives


def test_foreground_suggestion_is_only_applied_when_the_user_allowed_it():
    svc = GestureService(screen_provider=lambda _m: SCREEN)
    svc.foreground_probe = lambda: "chrome.exe"
    svc.check_foreground()
    assert svc.suggested == "browsing" and svc.profile == "general"                  # suggested, not applied
    svc.settings.auto_profile = True
    svc.check_foreground()
    assert svc.profile == "browsing" and svc.settings.swipe == "browser"
    svc.foreground_probe = lambda: (_ for _ in ()).throw(OSError("no window"))       # a probe failure is harmless
    svc.check_foreground()
    assert svc.profile == "browsing"


# ---------------------------------------------------------------- macros
def test_macros_persist_and_a_bad_entry_is_skipped(tmp_path):
    reg = sf.MappingRegistry()
    reg.add("SWIPE_LEFT", "run_workflow", {"name": "focus"}, profile="presentation", level=1, note="x")
    reg.items["SWIPE_LEFT"]["level"] = 1
    store = macros.MacroStore(tmp_path / "m.json")
    store.save(reg, "presentation")
    back = sf.MappingRegistry()
    assert store.load(back) == "presentation"
    assert back.items["SWIPE_LEFT"]["args"] == {"name": "focus"} and back.items["SWIPE_LEFT"]["profile"] == "presentation"
    (tmp_path / "m.json").write_text('{"macros": {"PINCH": {"tool": "x"}, "FIST": {"tool": "run_command"}, "SWIPE_RIGHT": 5}}', encoding="utf-8")
    fresh = sf.MappingRegistry()
    store.load(fresh)
    assert fresh.items == {}                                                           # every invalid entry dropped
    assert macros.MacroStore(tmp_path / "missing.json").load(fresh) == ""


def test_macro_risk_and_scope_rules():
    reg = sf.MappingRegistry()
    with pytest.raises(ValueError, match="too risky"):
        reg.add("FIST", "format_disk", level=3)
    reg.add("SWIPE_RIGHT", "media_control", profile="media")
    assert reg.due("SWIPE_RIGHT", "general") is None and reg.due("SWIPE_RIGHT", "media")
    assert reg.owned_swipes("media") == {"SWIPE_RIGHT"} and reg.owned_swipes("general") == set()
    assert "media_control" in macros.preview("SWIPE_RIGHT", "media_control", {}, level=0, profile="media", hold_s=0, cooldown_s=2)


def test_a_macro_owning_a_swipe_stops_the_swipe_key_pair():
    dev = sf.FakeInput()
    from app.gesture.cursor import Calibration, CursorConfig, VirtualCursor

    ctl = sf.SafetyController(dev, VirtualCursor(SCREEN, CursorConfig(mirror=False), Calibration(0, 0, 1, 1)))
    ctl.initializing(), ctl.ready(), ctl.activate()
    ctl.swipe_mode = "browser"
    ctl.process(eng.GestureFrame(events=[("swipe", "left")]))
    assert ("hotkey", "alt+left") in dev.log
    dev.log.clear()
    ctl.macro_owned = {"SWIPE_LEFT"}
    ctl.process(eng.GestureFrame(events=[("swipe", "left")]))
    ctl.process(eng.GestureFrame(events=[("swipe", "right")]))
    assert dev.log == [("hotkey", "alt+right")]


def test_service_dispatch_runs_a_swipe_macro_but_never_in_practice_and_fist_needs_a_hold():
    svc = GestureService(screen_provider=lambda _m: SCREEN)
    ran = []
    svc.run_action = lambda tool, args, confirm: ran.append((tool, args))
    svc.mapping.add("SWIPE_LEFT", "run_workflow", {"name": "w"})
    svc.mapping.add("FIST", "open_app", {"app": "x"}, hold_s=0.8)
    now = [0.0]
    svc.clock = lambda: now[0]
    svc.mapping.clock = lambda: now[0]
    svc._dispatch(eng.GestureFrame(events=[("swipe", "left")]))
    assert ran == [("run_workflow", {"name": "w"})]
    for t in (10.0, 10.2, 10.5):                                    # fist held only 0.5 s: nothing
        now[0] = t
        svc._dispatch(eng.GestureFrame(gesture="FIST"))
    assert len(ran) == 1
    now[0] = 11.0
    svc._dispatch(eng.GestureFrame(gesture="FIST"))
    assert ran[-1] == ("open_app", {"app": "x"})
    svc.practice, now[0] = True, 30.0
    svc._dispatch(eng.GestureFrame(events=[("swipe", "left")]))
    assert len(ran) == 2


def test_run_action_reaches_the_event_loop_from_a_camera_thread():
    """The old bridge started a task from the camera thread (no loop there) and silently dropped the call."""
    from app.ai.base import ToolCall
    from app.tools import gesture_tool

    seen = []

    class Exec:
        registry = None

        async def execute(self, call: ToolCall, **kw):
            seen.append((call.name, kw))

    class Ctx:
        executor, paths, database = Exec(), None, None

    async def scenario():
        gesture_tool.SERVICE_RESET = None
        from app.gesture.service import SERVICE

        SERVICE.run_action = None
        SERVICE.foreground_probe = lambda: ""
        gesture_tool._wire(Ctx())
        t = threading.Thread(target=lambda: SERVICE.run_action("media_control", {"action": "next"}, True))
        t.start(), t.join()
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert seen == [("media_control", {"user_command": False})]


# ---------------------------------------------------------------- benchmark
def test_benchmark_is_deterministic_and_clean_on_the_current_engine():
    a, b = metrics.run_suite(), metrics.run_suite()
    assert {k: v for k, v in a.items() if k != "ms_per_frame_median" and k != "ms_per_frame_max"} == \
           {k: v for k, v in b.items() if k != "ms_per_frame_median" and k != "ms_per_frame_max"}
    assert a["false_activations"] == 0 and a["missed"] == 0 and a["stuck_buttons"] == 0 and a["recognition_accuracy"] == 1.0
    assert a["synthetic"] is True


def test_benchmark_stays_clean_as_jitter_rises():
    for result in metrics.sweep():
        assert result["false_activations"] == 0 and result["stuck_buttons"] == 0, result["noise"]


def test_benchmark_measures_the_improvement_against_the_old_pinch_rule():
    """pinch_confirm=1 is the previous behaviour (one frame under the threshold clicks)."""
    old = metrics.run_suite(config={"pinch_confirm": 1})
    new = metrics.run_suite()
    assert old["false_activations"] >= 3 and new["false_activations"] == 0
    assert metrics.compare(old, new) == []                                  # better is not a regression
    assert any("false activations" in p for p in metrics.compare(new, old))   # worse is caught


def test_benchmark_catches_a_release_failure():
    """A hand leaving mid-drag must not leave the button down."""
    sc = metrics.Scenario("drag then vanish", [synthetic_hand()] * 10 + [synthetic_hand(pinch=True)] * 40 + [None] * 20, {"click": 0})
    out = metrics.run_scenario(sc, config={"drag_enabled": True})
    assert not out.left_button_down_at_end


def test_profile_phrases():
    from app.agent.shortcuts import match_gesture

    assert match_gesture("switch gesture profile to presentation").arguments == {"action": "profile", "profile": "presentation"}
    assert match_gesture("browsing mode for gestures").arguments["profile"] == "browsing"
    assert match_gesture("show gesture profiles").arguments == {"action": "profiles"}
    assert match_gesture("switch gestures profile to nonsense") is None
