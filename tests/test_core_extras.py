"""Core extras: interface sounds, real speech level, data pulses + spark trails, hidden panels, HUD scale setting."""

import math

import numpy as np
import pytest

from app.ui import sound_fx
from app.ui.core3d import fx


def test_every_sound_is_short_quiet_and_click_free():
    for kind in sound_fx.KINDS:
        wave = sound_fx.synth(kind)
        assert wave.dtype == np.float32 and 0.05 < wave.size / sound_fx.RATE < 0.5
        assert np.abs(wave).max() <= 1.0 and abs(float(wave[-1])) < 1e-3
    with pytest.raises(ValueError):
        sound_fx.synth("nope")


def test_sounds_respect_setting_busy_and_rate_limit():
    played, now = [], [0.0]
    state = {"on": False, "busy": False}
    fxs = sound_fx.SoundFx(played.append, enabled=lambda: state["on"], busy=lambda: state["busy"],
                           volume=lambda: 0.5, clock=lambda: now[0])
    assert not fxs.play("tool")                       # off by default
    state["on"] = True
    assert fxs.play("tool") and len(played) == 1
    assert not fxs.play("tool")                       # too soon after the last tick
    now[0] = 1.0
    state["busy"] = True
    assert not fxs.play("tool")                       # never while speaking / listening
    state["busy"] = False
    assert fxs.play("tool") and np.abs(played[-1].samples).max() <= 0.5 + 1e-6


def test_comet_runs_from_core_to_panel_and_fades():
    start, end = (0.0, 0.0), (100.0, 0.0)
    early, late = fx.comet(start, end, 0.1), fx.comet(start, end, 0.99)
    assert early[0][0] < late[0][0] <= 100.0
    assert late[0][2] < early[0][2]                   # fades out at the end
    assert all(a >= b for (_, _, a, _), (_, _, b, _) in zip(early, early[1:]))   # trail fades behind the head
    assert fx.nearest_point((10, 10, 20, 20), (0, 0)) == (10, 10)


def test_spark_trails_circle_the_core():
    trails = fx.spark_trails((50.0, 50.0), 40.0, clock=1.0)
    assert len(trails) == fx.SPARKS and all(len(t) == fx.TRAIL_POINTS for t in trails)
    for trail in trails:
        x, y, alpha, size = trail[0]
        assert 10 < math.hypot(x - 50, y - 50) < 70 and alpha >= trail[-1][2]


def test_pulses_expire_and_are_capped():
    e = fx.Effects()
    for _ in range(20):
        e.pulse(object(), 0.0)
    assert len(e.pulses) == fx.MAX_PULSES
    assert e.live(fx.PULSE_SECONDS + 0.01) == []
    e.pulses_on = False
    e.pulse(object(), 0.0)
    assert e.pulses == []


def test_speech_level_follows_the_played_audio():
    from app.tts.manager import TTSManager

    class Player:
        def play(self, clip, volume=1.0, abort=None, on_chunk=None):
            pass

    m = TTSManager.__new__(TTSManager)
    import collections

    m._levels = collections.deque(maxlen=200)
    m._player_reports_levels = True
    assert m.speech_level() is None                   # nothing played recently: the core uses its own cadence
    import time

    m._levels.append((time.monotonic(), 0.3))
    loud = m.speech_level()
    m._levels.append((time.monotonic(), 0.003))
    assert 0.0 <= m.speech_level() < loud <= 1.0
    m._player_reports_levels = False
    assert m.speech_level() is None


def test_hidden_panels_and_settings():
    from app.config.live_fields import STARTUP_ONLY_FIELDS
    from app.config.settings import Settings
    from app.ui.dashboard_fx import hidden_titles

    assert hidden_titles(" Power core, link monitor ,") == {"power core", "link monitor"}
    s = Settings()
    assert s.core_pulses and s.core_trails and not s.ui_sounds and s.hud_scale == 1.0 and s.hidden_panels == ""
    assert "hud_scale" in STARTUP_ONLY_FIELDS
