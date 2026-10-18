"""Steering a running request: pause / cancel a mission at the next step, 'stop speaking', 'explain what you are
doing', 'show me the plan', "don't remember this" - and a mission stuck in 'verifying' recovers after a restart."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.ai.base import ToolCall
from app.core import live_control, missions
from app.core.missions import MissionStore, validate_plan
from tests.test_v350 import db, make, steps  # noqa: F401  (fixture + helpers: a real executor and tools)


@pytest.mark.parametrize("text,intent", [
    ("stop speaking", "stop_speaking"), ("Jarvis, stop talking please", "stop_speaking"), ("চুপ করো", "stop_speaking"),
    ("pause this mission", "pause_mission"), ("hold the task", "pause_mission"),
    ("cancel the current task", "cancel_task"), ("stop what you're doing", "cancel_task"),
    ("explain what you are doing", "explain_now"), ("what are you doing now?", "explain_now"),
    ("show me the plan before continuing", "show_plan"), ("what's the plan", "show_plan"),
    ("do not remember this conversation", "dont_remember"), ("don't save this", "dont_remember"),
    ("open notepad", None), ("stop the music", None), ("explain quantum physics to me", None),
])
def test_classify(text, intent):
    assert live_control.classify(text) == intent


def _running(db, ex, kind):  # noqa: F811
    store = MissionStore(db)
    mission = store.create("long job", validate_plan(steps(("look", "a"), ("look", "b"), ("look", "c")),
                                                     ex.registry, ex.context))
    seen = []

    def on_step(m, step):
        seen.append(step.n)
        if step.n == 2:
            missions.request(m.id, kind)          # said while step 2 runs

    outcome = asyncio.run(missions.run(mission, store, ex, approved=True, on_step=on_step))
    return store, mission, outcome, seen


def test_pause_stops_before_the_next_step_and_can_continue(db):  # noqa: F811
    ex, _, log = make(db)
    store, mission, outcome, seen = _running(db, ex, "pause")
    assert not outcome.ok and "Paused 'long job' before step 3" in outcome.message
    assert log == ["look:a", "look:b"] and store.get(mission.id).state == "paused"   # step 2 finished, 3 didn't run
    again = asyncio.run(missions.run(store.get(mission.id), store, ex, approved=True))
    assert again.ok and log == ["look:a", "look:b", "look:c"]                      # nothing repeated


def test_cancel_stops_and_stays_cancelled(db):  # noqa: F811
    ex, _, log = make(db)
    store, mission, outcome, _ = _running(db, ex, "cancel")
    assert "Cancelled 'long job' before step 3" in outcome.message and log == ["look:a", "look:b"]
    assert store.get(mission.id).state == "cancelled"
    assert asyncio.run(missions.run(store.get(mission.id), store, ex, approved=True)).message.endswith("cancelled.")


def test_cancel_tool_on_a_running_mission_asks_it_to_stop(db):  # noqa: F811
    ex, _, _ = make(db)
    store = MissionStore(db)
    mission = store.create("job", validate_plan(steps(("look", "a")), ex.registry, ex.context))
    store.set_state(mission, "running")
    result = asyncio.run(ex.execute(ToolCall("cancel_mission", {}))).result
    assert result.ok and "before its next step" in result.message
    assert missions._take_request(mission.id) == "cancel" and store.get(mission.id).state == "running"


def test_verifying_is_recovered_after_a_restart(db):  # noqa: F811
    ex, _, _ = make(db)
    store = MissionStore(db)
    mission = store.create("job", validate_plan(steps(("look", "a")), ex.registry, ex.context))
    store.set_state(mission, "running")
    store.set_state(mission, "verifying")
    assert store.unfinished().id == mission.id and store.mark_interrupted() == 1
    assert store.get(mission.id).state == "paused"


def test_plan_text_marks_where_it_is(db):  # noqa: F811
    ex, _, _ = make(db)
    mission = MissionStore(db).create("job", validate_plan(steps(("look", "a"), ("look", "b")), ex.registry, ex.context))
    mission.steps[0].state, mission.steps[1].state = "done", "running"
    text = live_control.plan_text(mission)
    assert "1. look a [done]" in text and "2. look b [NOW]" in text


def _host(busy=True, state="EXECUTING"):
    from app.ui.controller_parts.features import FeaturesMixin

    host = FeaturesMixin.__new__(FeaturesMixin)
    said, notices = [], []
    host._busy, host._state = busy, state
    host.user_message = SimpleNamespace(emit=said.append)
    host.notice = SimpleNamespace(emit=notices.append)
    stops = []
    host.tts = SimpleNamespace(stop=lambda: stops.append(1))
    host.ctx = SimpleNamespace(database=None)
    host.cancel = lambda: stops.append("cancel")
    host._mode_control = lambda name: f"{name} mode"
    return host, notices, stops


def test_window_handles_control_words_while_busy():
    host, notices, stops = _host()
    assert host._live_control("stop speaking") and stops == [1] and "still running" in notices[-1]
    assert host._live_control("cancel the current task") and "cancel" in stops
    assert host._live_control("pause this mission") and "No mission is running" in notices[-1]
    assert host._live_control("what are you doing") and notices[-1].startswith("I'm running an action")
    assert host._live_control("don't remember this") and "private mode" in notices[-1]
    assert not host._live_control("open notepad")                          # an ordinary request: still refused


def test_idle_only_stop_speaking_and_private_are_taken():
    host, notices, stops = _host(busy=False, state="IDLE")
    assert not host._live_control("pause this mission")                  # idle: the normal tools answer it
    assert host._live_control("stop speaking") and stops == [1]
