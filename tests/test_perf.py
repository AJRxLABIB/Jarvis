"""Performance modes: decisions from measured numbers (made-up here), router shift, chores, gesture cap, fit explanations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import perf
from app.core.perf import Snapshot

PLUGGED = Snapshot(ram_total_gb=31.7, ram_free_gb=18.0, cpu_pct=12, battery_pct=80, on_battery=False, disk_free_gb=200,
                   vram_total_gb=6.0, vram_free_gb=5.0)


def test_normalise_names():
    assert perf.normalise("High Quality") == "quality" and perf.normalise("battery-saver") == "battery"
    assert perf.normalise("nonsense") == "balanced" and perf.normalise(None) == "balanced" and perf.normalise("auto") == "auto"


def test_auto_follows_measurements_and_explicit_choices_are_never_overridden():
    low_battery = Snapshot(battery_pct=20, on_battery=True, ram_free_gb=10, cpu_pct=10)
    assert perf.effective("auto", low_battery)[0] == "battery" and "20%" in perf.effective("auto", low_battery)[1]
    assert perf.effective("auto", Snapshot(ram_free_gb=1.2))[0] == "fast"
    assert perf.effective("auto", Snapshot(cpu_pct=95, ram_free_gb=10))[0] == "fast"
    assert perf.effective("auto", PLUGGED)[0] == "balanced"
    assert perf.effective("auto", Snapshot())[0] == "balanced"                      # nothing measured: no guess
    assert perf.effective("quality", low_battery)[0] == "quality"                    # you asked for it, you get it


def test_plans():
    q, f, b = perf.plan("quality", PLUGGED), perf.plan("fast", PLUGGED), perf.plan("battery", PLUGGED)
    assert (q.route_shift, f.route_shift, b.route_shift) == (1, -1, -1)
    assert q.allow_thinking and not f.allow_thinking and not b.allow_thinking
    assert b.defer_background and b.gesture_fps == 15 and b.gesture_hands == 1 and not q.defer_background and q.gesture_fps == 30
    busy = perf.plan("balanced", Snapshot(cpu_pct=92))
    assert busy.defer_background and not busy.allow_thinking and busy.gesture_fps <= 20
    assert "Battery saver" in b.describe()


def test_shift_route_only_goes_where_a_model_is_configured():
    q, f = perf.plan("quality", PLUGGED), perf.plan("fast", PLUGGED)
    assert perf.shift_route("normal", q, {"smart"})[0] == "smart"
    assert perf.shift_route("normal", q, set())[0] == "normal"                       # no smart model: nothing to move to
    assert perf.shift_route("smart", f, {"fast"})[0] == "normal"
    assert perf.shift_route("normal", f, set())[0] == "fast" or perf.shift_route("normal", f, {"fast"})[0] == "fast"
    assert perf.shift_route("fast", f, {"fast"})[0] == "fast"                        # already the smallest
    assert perf.shift_route("coding", q, {"smart"})[0] == "coding"                   # coding keeps its model


def test_fit_explanations_use_numbers_and_never_claim_without_data():
    ok, why = perf.fit_check(2.0, PLUGGED)
    assert ok is True and "GPU memory" in why
    ok, why = perf.fit_check(5.5, PLUGGED)                                           # 6.3 GB needed, 5.0 free on the GPU, plenty of RAM
    assert ok is True and "CPU" in why and "slower" in why
    ok, why = perf.fit_check(30.0, PLUGGED)
    assert ok is False and "smaller model" in why
    assert perf.fit_check(None, PLUGGED)[0] is None and perf.fit_check(2.0, Snapshot())[0] is None
    no_gpu = Snapshot(ram_free_gb=8.0)
    assert perf.fit_check(3.0, no_gpu)[0] is True and "no NVIDIA GPU" in perf.fit_check(3.0, no_gpu)[1]


def test_snapshot_is_cached_and_a_reader_is_used_in_tests():
    calls = []
    def reader():
        calls.append(1)
        return PLUGGED
    assert perf.read_snapshot(reader=reader) is PLUGGED and calls == [1]
    perf._CACHE = None
    first = perf.read_snapshot(now=100.0)
    assert perf.read_snapshot(now=110.0) is first                                    # inside max_age: no second measurement
    perf._CACHE = None


def test_conversation_applies_the_mode_to_the_route(monkeypatch):
    from app.agent.conversation import ConversationManager
    from app.ai.router import Route

    monkeypatch.setattr(perf, "read_snapshot", lambda **k: PLUGGED)
    holder = SimpleNamespace(settings=SimpleNamespace(performance_mode="quality", ai_fast_model="", ai_smart_model="big"))
    out = ConversationManager._apply_performance(holder, Route("normal", "a normal request"))
    assert out.kind == "smart" and "High quality" in out.reason
    holder.settings.ai_smart_model = ""
    assert ConversationManager._apply_performance(holder, Route("normal", "x")).kind == "normal"


def test_upkeep_postpones_the_backup_but_never_for_more_than_three_days(tmp_path, monkeypatch):
    from app.core.upkeep import Upkeep
    from app.memory import backup

    monkeypatch.setattr(perf, "read_snapshot", lambda **k: Snapshot(battery_pct=10, on_battery=True, ram_free_gb=9, cpu_pct=5))
    up = Upkeep.__new__(Upkeep)
    up.settings, up.backup_folder = SimpleNamespace(performance_mode="battery"), tmp_path
    now = datetime(2026, 10, 29, 12, 0)
    assert up._defer(now) is False                                                  # no backup exists: never postponed
    (tmp_path / f"{backup.PREFIX}20261028-120000.db").write_bytes(b"x")
    assert up._defer(now) is True                                                    # yesterday's backup: can wait
    (tmp_path / f"{backup.PREFIX}20261028-120000.db").unlink()
    (tmp_path / f"{backup.PREFIX}20261024-120000.db").write_bytes(b"x")
    assert up._defer(now) is False                                                   # 5 days old: must run now


def test_gesture_service_obeys_the_cap():
    from app.gesture.service import GestureService

    svc = GestureService()
    svc.limits_provider = lambda: (15.0, 1)
    assert svc._limits() == (15.0, 1)
    svc.limits_provider = lambda: (30.0, 2)
    assert svc._limits() == (0.0, 2)                                                 # 30 is the camera rate: no cap
    svc.limits_provider = lambda: 1 / 0
    assert svc._limits() == (0.0, 2)                                                 # a broken provider never blocks gestures


def test_tool_status_set_and_fit(monkeypatch):
    from app.tools import performance_tool as pt

    monkeypatch.setattr(perf, "read_snapshot", lambda **k: PLUGGED)
    applied = {}
    models = [SimpleNamespace(name="tiny:1b", size_bytes=int(1e9)), SimpleNamespace(name="huge:70b", size_bytes=int(40e9))]

    class AI:
        async def models(self): return models
        async def resolve_model(self): return models[0], ""

    ctx = SimpleNamespace(settings=SimpleNamespace(performance_mode="balanced"), apply_setting=lambda k, v: applied.update({k: v}), ai=AI())
    run = lambda **kw: asyncio.run(pt.performance_mode(pt.PerfArgs(**kw), ctx))      # noqa: E731
    assert "Balanced" in run(action="status").message and "memory 18.0 of 31.7 GB free" in run(action="status").message
    assert run(action="set", mode="battery saver").ok and applied == {"performance_mode": "battery"}
    with pytest.raises(Exception):
        run(action="set", mode="warp")
    assert run(action="fit", model="tiny").data["fits"] is True
    assert run(action="fit", model="huge:70b").data["fits"] is False
    assert run(action="fit").data["model"] == "tiny:1b"


def test_phrases():
    from app.agent.shortcuts import match_performance as m

    assert m("battery saver mode").arguments["mode"] == "battery" and m("switch to high quality mode").arguments["mode"] == "quality"
    assert m("set performance mode to auto").arguments["mode"] == "auto"
    assert m("will llama3.2:3b fit").arguments == {"action": "fit", "model": "llama3.2:3b"}
    assert m("does the model fit in memory").arguments["action"] == "fit"
    assert m("will it fit") is None and m("performance mode") is None             # bare "performance mode" is the older settings profile
