"""The start-up crash guard for the OpenGL core (app/ui/core3d/gl_guard.py) and the no-fade rule for the GL window."""

from types import SimpleNamespace

from app.ui.core3d import gl_guard


def _settings(**kw):
    base = {"visual_quality": "auto", "core_renderer": "auto"}
    base.update(kw)
    return SimpleNamespace(**base)


def test_clean_runs_never_step_down(tmp_path):
    assert gl_guard.decide(tmp_path, previous_crashed=False) == 0
    gl_guard.mark(tmp_path, gl_guard.SHOWING)
    gl_guard.mark(tmp_path, gl_guard.STABLE)
    assert gl_guard.decide(tmp_path, previous_crashed=True) == 0      # crashed later, not while the GL core started
    assert gl_guard.overrides(0, _settings()) == {}


def test_crash_after_window_shown_steps_down_then_painter(tmp_path):
    gl_guard.decide(tmp_path, False)
    gl_guard.mark(tmp_path, gl_guard.SHOWING)
    strikes = gl_guard.decide(tmp_path, previous_crashed=True)
    assert strikes == 1
    first = gl_guard.overrides(strikes, _settings())
    assert first == {"intro_animation": False, "visual_quality": "balanced"}
    assert "flat core" not in gl_guard.notice(strikes, first)
    gl_guard.mark(tmp_path, gl_guard.SHOWING)
    strikes = gl_guard.decide(tmp_path, previous_crashed=True)
    second = gl_guard.overrides(strikes, _settings())
    assert second["core_renderer"] == "painter"
    assert "--reset-gl-guard" in gl_guard.notice(strikes, second)


def test_explicit_choices_are_respected(tmp_path):
    assert "core_renderer" not in gl_guard.overrides(3, _settings(core_renderer="gl"))
    assert "visual_quality" not in gl_guard.overrides(1, _settings(visual_quality="low"))


def test_stable_with_full_settings_clears_but_downgraded_keeps(tmp_path):
    gl_guard.mark(tmp_path, gl_guard.SHOWING)
    assert gl_guard.decide(tmp_path, True) == 1
    gl_guard.mark(tmp_path, gl_guard.STABLE, full_settings=False)
    assert gl_guard.decide(tmp_path, False) == 1                       # still stepped down until reset
    gl_guard.mark(tmp_path, gl_guard.STABLE, full_settings=True)
    assert gl_guard.decide(tmp_path, False) == 0
    assert gl_guard.reset(tmp_path) and gl_guard.load(tmp_path) == {}


def test_broken_file_is_ignored(tmp_path):
    (tmp_path / gl_guard.FILE).write_text("{not json", encoding="utf-8")
    assert gl_guard.decide(tmp_path, True) == 0


def test_gl_window_never_uses_window_opacity_fades():
    from app.ui.main_window import MainWindow

    assert MainWindow._can_fade(SimpleNamespace(_gl_backdrop=True)) is False
    assert MainWindow._can_fade(SimpleNamespace(_gl_backdrop=False)) is True
