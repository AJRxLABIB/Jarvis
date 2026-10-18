"""The 3D core: state machine, quality governor, geometry, shaders, the GL render (when a GL context exists) and the
fallback to the painter-drawn core."""

from __future__ import annotations

import math
import os

import numpy as np
import pytest

from app.ui.core3d import shaders
from app.ui.core3d.dynamics import MAX_SPEED, TARGETS, CoreDynamics, canonical
from app.ui.core3d.quality import LEVELS, QUALITY, Governor, resolve


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ------------------------------------------------------------------ dynamics
def run(dyn: CoreDynamics, seconds: float, fps: int = 60, level=None) -> None:
    for _ in range(int(seconds * fps)):
        dyn.step(1 / fps, level)


def test_every_app_state_maps_to_a_core_state() -> None:
    for state in ("IDLE", "LISTENING", "THINKING", "EXECUTING", "SPEAKING", "ERROR", "WAITING_FOR_PERMISSION", "STARTING"):
        assert canonical(state) in TARGETS
    assert canonical("THINKING") == "PROCESSING" and canonical("nonsense") == "IDLE"


def test_state_changes_glide_instead_of_jumping() -> None:
    dyn = CoreDynamics()
    run(dyn, 2.0)
    idle_speed = dyn.speed
    dyn.set_state("THINKING")
    dyn.step(1 / 60)
    assert dyn.speed < 1.2 * idle_speed + 0.2                     # one frame later: still near the old speed
    run(dyn, 3.0)
    assert dyn.speed > 2.0 * idle_speed and dyn.speed <= MAX_SPEED  # eased up to the fast spin, never beyond the cap
    dyn.set_state("IDLE")
    run(dyn, 4.0)
    assert abs(dyn.speed - idle_speed) < 0.1 and dyn.label == "READY"


def test_shader_clock_never_runs_backwards_and_survives_long_pauses() -> None:
    dyn = CoreDynamics()
    last = 0.0
    for state in ("IDLE", "PROCESSING", "SPEAKING", "ERROR", "IDLE"):
        dyn.set_state(state)
        for _ in range(60):
            dyn.step(1 / 60)
            assert dyn.phase >= last
            last = dyn.phase
    before = dyn.phase
    dyn.step(30.0)                                                # a suspended laptop must not leap ahead
    assert dyn.phase - before < 1.0


def test_states_have_distinct_looks_and_errors_are_red() -> None:
    tints, speeds = {}, {}
    for state in TARGETS:
        dyn = CoreDynamics()
        dyn.set_state(state)
        run(dyn, 4.0)
        tints[state], speeds[state] = dyn.tint, dyn.speed
    assert tints["ERROR"][0] > tints["ERROR"][2] - 0.2 and tints["ERROR"][0] > 0.8          # red
    assert tints["WAITING_FOR_PERMISSION"][0] > tints["WAITING_FOR_PERMISSION"][2]          # amber
    assert speeds["PROCESSING"] > speeds["LISTENING"] > speeds["ERROR"]


def test_reduced_motion_and_user_speed_scale_apply() -> None:
    calm, normal, quick = CoreDynamics(reduced_motion=True), CoreDynamics(), CoreDynamics(speed_scale=2.0)
    for d in (calm, normal, quick):
        d.set_state("PROCESSING")
        run(d, 4.0)
    assert calm.speed < 0.4 * normal.speed and quick.speed > normal.speed and quick.speed <= MAX_SPEED
    assert calm.breath < 1e-6


def test_speaking_without_measured_audio_uses_a_synthetic_cadence_and_listening_uses_the_mic() -> None:
    speaking = CoreDynamics()
    speaking.set_state("SPEAKING")
    run(speaking, 1.0)
    assert 0.0 < speaking.level < 0.7
    listening = CoreDynamics()
    listening.set_state("LISTENING")
    run(listening, 1.0, level=0.9)
    assert listening.level > 0.7
    run(listening, 3.0, level=None)                               # the microphone went quiet: level decays
    assert listening.level < 0.1


# ------------------------------------------------------------------ quality
def test_quality_levels_and_resolution() -> None:
    assert list(LEVELS) == ["low", "balanced", "high"]
    counts = [QUALITY[n].shell + QUALITY[n].nucleus for n in LEVELS]
    assert counts == sorted(counts) and QUALITY["high"].bloom and not QUALITY["low"].bloom
    assert resolve("high").name == "high" and resolve("auto", "low").name == "low" and resolve("bogus").name == "balanced"
    assert QUALITY["high"].fps >= QUALITY["balanced"].fps > QUALITY["low"].fps and QUALITY["balanced"].samples >= 4


def test_governor_judges_frame_cost_not_the_gap_between_frames() -> None:
    budget = 1 / 60
    cheap = Governor("high", window=1.0, cooldown=2.0)
    # 60 fps with 4 ms of drawing each: plenty of headroom - the old bug downgraded exactly this case
    assert [c for c in (cheap.frame(0.004, budget) for _ in range(600)) if c] == [] and cheap.level == "high"


def test_governor_steps_down_when_heavy_never_returns_to_that_level_and_does_not_flap() -> None:
    budget = 1 / 60
    g = Governor("high", window=1.0, cooldown=3.0)
    changes = [c for c in (g.frame(0.015, budget) for _ in range(70)) if c]   # 90% of the budget
    assert changes == ["balanced"]
    assert [c for c in (g.frame(0.015, budget) for _ in range(120)) if c] == []   # cooldown: no second step yet
    assert [c for c in (g.frame(0.015, budget) for _ in range(400)) if c] == ["low"]
    assert [c for c in (g.frame(0.001, budget) for _ in range(3000)) if c] == []   # never back to a level it left
    assert g.level == "low"
    one = Governor("high", window=1.0, cooldown=2.0)
    for _ in range(70):
        one.frame(0.015, budget)                                                 # high was too heavy -> balanced
    assert one.level == "balanced"
    assert [c for c in (one.frame(0.001, budget) for _ in range(3000)) if c] == [] and one.level == "balanced"
    assert Governor("balanced").frame(0.01, budget, dt=5.0) is None             # a pause is not a slow frame


def test_governor_climbs_to_high_on_a_fast_machine() -> None:
    g = Governor("low", window=1.0, cooldown=2.0)
    for _ in range(4000):
        g.frame(0.001, 1 / 60)
    assert g.level == "high"


# ------------------------------------------------------------------ geometry and shaders
def test_geometry_is_well_formed() -> None:
    from app.ui.core3d.scene import RING_SPECS, filament_data, particle_data, ring_data

    rng = np.random.default_rng(1)
    for shell in (False, True):
        data = particle_data(rng, 500, shell)
        assert data.shape == (500, 11) and data.dtype == np.float32 and np.isfinite(data).all()
        assert np.allclose(np.linalg.norm(data[:, :3], axis=1), 1.0, atol=1e-4)
    rows, index = filament_data(rng, 40, 12)
    assert rows.shape == (40 * 13, 13) and index.max() < len(rows) and index.size == 40 * 12 * 2
    assert rows[:, 0].min() == 0.0 and rows[:, 0].max() == 1.0
    lines, slices, ticks, markers = ring_data(rng, RING_SPECS[:5])
    assert len(slices) == 5 and sum(c for _, c in slices) == len(lines) and len(markers) == 15
    assert len(ticks) % 2 == 0 and np.isfinite(ticks).all()


def test_every_shader_declares_its_version_and_main() -> None:
    for name in ("POINTS_VS", "POINTS_FS", "FIL_VS", "FIL_FS", "RING_VS", "RING_FS", "FULLSCREEN_VS", "GLOW_FS",
                 "BLUR_FS", "BRIGHT_FS", "COMBINE_FS"):
        src = getattr(shaders, name)
        assert src.lstrip().startswith("#version 330 core") and "void main()" in src, name


def test_matrices() -> None:
    from app.ui.core3d.scene import perspective, rotation, translation

    p = perspective(38, 1.0)
    point = p @ translation(0, 0, -5.6) @ np.array([0, 0, 0, 1.0])
    assert point[3] > 0 and abs(point[0] / point[3]) < 1e-9
    r = rotation("y", math.pi / 2) @ np.array([1, 0, 0, 1.0])
    assert np.allclose(r[:3], [0, 0, -1], atol=1e-9)


# ------------------------------------------------------------------ the real GL render (needs a GL context: skipped otherwise)
def _gl_context():
    try:
        import moderngl
    except ImportError:
        pytest.skip("moderngl is not installed")
    try:
        return moderngl.create_standalone_context(libgl=os.environ.get("JARVIS_TEST_LIBGL") or None)
    except Exception as exc:
        pytest.skip(f"no OpenGL context here ({str(exc)[:60]})")


@pytest.mark.parametrize("level", ["low", "balanced", "high"])
def test_scene_compiles_and_draws_something_at_every_quality(level) -> None:
    from app.ui.core3d.scene import CoreScene

    ctx = _gl_context()
    fbo = ctx.framebuffer([ctx.texture((200, 200), 4)])
    scene = CoreScene(ctx, QUALITY[level])
    dyn = CoreDynamics()
    run(dyn, 1.0, 30)
    scene.render(fbo, (200, 200), dyn, 0.1, 0.05)
    pixels = np.frombuffer(fbo.read(components=4), dtype=np.uint8).reshape(200, 200, 4)
    assert pixels[..., :3].max() > 60 and (pixels[..., 3] > 0).mean() > 0.05    # the globe is there
    centre, corner = int(pixels[90:110, 90:110, :3].mean()), int(pixels[:8, :8, :3].mean())
    assert centre > corner + 5                                                  # brighter in the middle than the corner
    scene.build(QUALITY["low"])                                                 # rebuilding releases and recreates cleanly
    scene.render(fbo, (200, 200), dyn, 0.0, 0.0)
    scene.release()


def test_every_state_renders_without_error() -> None:
    from app.ui.core3d.scene import CoreScene

    ctx = _gl_context()
    fbo = ctx.framebuffer([ctx.texture((128, 128), 4)])
    scene = CoreScene(ctx, QUALITY["balanced"])
    for state in TARGETS:
        dyn = CoreDynamics()
        dyn.set_state(state)
        run(dyn, 0.5, 30, level=0.5)
        scene.render(fbo, (128, 128), dyn, 0.0, 0.0)
    scene.release()


# ------------------------------------------------------------------ widget / fallback
def test_create_core_uses_the_painter_core_without_a_display_and_when_asked(qapp, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.ui.core3d.widget import create_core, gl_available
    from app.ui.visualizer import OrbWidget

    assert gl_available()[0] is False or os.environ.get("QT_QPA_PLATFORM", "") != "offscreen"
    assert type(create_core(SimpleNamespace(core_renderer="painter"))) is OrbWidget
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert type(create_core(SimpleNamespace(core_renderer="gl"))) is OrbWidget       # no display: never a GL widget


def test_a_failed_gl_core_keeps_working_as_the_painter_core(qapp) -> None:
    from PySide6.QtGui import QPixmap

    from app.ui.core3d.widget import GlOrbWidget

    w = GlOrbWidget("balanced")
    seen = []
    w.failed.connect(seen.append)
    w._fail("simulated driver failure")
    assert w.gl_ok is False and seen == ["simulated driver failure"]
    w.set_state("SPEAKING")
    w.set_level(-20.0)
    w.set_telemetry("cpu", 40, "40%")
    assert w.state == "SPEAKING" and w.state_label == "SPEAKING"
    pix = QPixmap(300, 300)
    from PySide6.QtGui import QPainter

    painter = QPainter(pix)
    w.model.resize(300, 300)
    w.model.paint_full(painter, 300, 300)                                          # the fallback drawing
    painter.end()
    w.apply_settings("low", 1.5, 1.2, True)
    assert w.quality.name == "low" and w.dyn.speed_scale == 1.5 and w.dyn.reduced_motion is True


def test_core_settings_validate_and_have_defaults() -> None:
    from pydantic import ValidationError

    from app.config.settings import Settings

    s = Settings()
    assert (s.core_renderer, s.visual_quality, s.core_speed, s.glow_intensity, s.reduced_motion) == ("auto", "auto", 1.0, 1.0, False)
    with pytest.raises(ValidationError):
        Settings(visual_quality="ultra")
    with pytest.raises(ValidationError):
        Settings(core_speed=9.0)


def test_holographic_geometry_spans_the_window_and_is_valid() -> None:
    from app.ui.core3d.backdrop import holo_geometry

    geo = holo_geometry(1920, 1080, 960, 500, 300, (0.3, 0.9, 1.0), [0.0, 0.5, 0.9] * 40, 1.0)
    assert geo.shape[1] == 6 and len(geo) % 3 == 0 and np.isfinite(geo).all()
    assert geo[:, 0].min() < 5 and geo[:, 0].max() > 1915                     # from edge to edge
    rgb, alpha = geo[:, 2:5], geo[:, 5]
    assert (rgb <= alpha[:, None] + 1e-6).all()                                # valid premultiplied colour
    narrow = holo_geometry(400, 300, 200, 150, 140, (1, 1, 1), [], 0.0)       # no room for the waves: streaks only
    assert len(narrow) > 0


def test_reference_look_geometry() -> None:
    """Dot-matrix globe (an even lattice on the unit sphere), swirl sheets inside it, gold/magenta orbits outside."""
    from app.ui.core3d.scene import ORBIT_SPECS, globe_data, orbit_data, swirl_data

    globe = globe_data(4000)
    assert globe.shape == (4000, 11) and np.allclose(np.linalg.norm(globe[:, :3], axis=1), 1.0, atol=1e-4)
    assert abs(globe[:, 1].mean()) < 0.01                                     # spread evenly, pole to pole
    swirls = swirl_data(np.random.default_rng(1), 6, 900)
    assert swirls.shape[1] == 11 and swirls[:, 3].max() < 1.0                 # inside the globe
    orbits = orbit_data(ORBIT_SPECS)
    assert len(orbits) >= sum(spec[5] for spec in ORBIT_SPECS) and (orbits[:, 10] == 2.0).any()   # a gold orbit
    assert orbits[:, 3].max() > 1.2                                           # it reaches beyond the globe


def test_bold_rings_circle_the_core_and_high_quality_supersamples() -> None:
    import math as m

    from app.ui.core3d.scene import rotate_about

    r = rotate_about((0.25, 1.0, 0.15), 1.0)
    assert np.allclose(r[:3, :3] @ r[:3, :3].T, np.eye(3), atol=1e-9)              # a true rotation
    k = np.array([0.25, 1.0, 0.15]) / np.linalg.norm([0.25, 1.0, 0.15])
    assert np.allclose(r[:3, :3] @ k, k)                                           # about that axis
    assert np.allclose(rotate_about((0, 0, 1), m.pi / 2)[:3, :3] @ [1, 0, 0], [0, 1, 0], atol=1e-9)
    assert QUALITY["high"].supersample > 1.0 and QUALITY["low"].supersample == 1.0


def test_streams_taper_fade_and_waves_follow_the_voice() -> None:
    from app.ui.core3d.backdrop import holo_geometry, ribbons

    line = np.stack([np.linspace(0, 100, 5), np.zeros(5)], axis=1)[None]
    colors = np.zeros((1, 5, 4))
    colors[..., :3] = 1.0
    colors[..., 3] = np.linspace(1.0, 0.0, 5)                                    # fades along its length
    tri = ribbons(line, np.array([[6.0, 5.0, 4.0, 3.0, 2.0]]), colors)          # and tapers
    assert tri.shape == (24, 6) and tri[0, 5] > tri[-1, 5]
    quiet = holo_geometry(1920, 1080, 960, 500, 300, (0.3, 0.9, 1.0), [0.0] * 120, 1.0)
    loud = holo_geometry(1920, 1080, 960, 500, 300, (0.3, 0.9, 1.0), [1.0] * 120, 1.0)
    assert np.ptp(loud[:, 1]) >= np.ptp(quiet[:, 1])                             # the waves swell with the voice
    rgb, alpha = loud[:, 2:5], loud[:, 5]
    assert (rgb <= alpha[:, None] + 1e-6).all() and np.isfinite(loud).all()
