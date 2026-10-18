from __future__ import annotations

from types import SimpleNamespace

from app import main
from app.utils import autostart


def _ctx(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AI_MODEL=qwen3:8b\n", encoding="utf-8")
    return SimpleNamespace(paths=SimpleNamespace(env_file=env), settings=SimpleNamespace(wake_word="hey jarvis")), env


def test_always_on_sets_tray_start_and_hands_free(tmp_path, monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(autostart, "set_enabled", calls.append)
    ctx, env = _ctx(tmp_path)
    assert main._always_on(ctx, True) == main.EXIT_OK
    text = env.read_text(encoding="utf-8")
    assert "START_WITH_WINDOWS=true" in text and "START_MINIMIZED=true" in text and "HANDS_FREE=true" in text
    assert "AI_MODEL=qwen3:8b" in text and calls == [True]
    assert "Hey Jarvis" in capsys.readouterr().out


def test_always_on_off_reverts(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(autostart, "set_enabled", calls.append)
    ctx, env = _ctx(tmp_path)
    main._always_on(ctx, True)
    assert main._always_on(ctx, False) == main.EXIT_OK
    assert "START_MINIMIZED=false" in env.read_text(encoding="utf-8") and calls == [True, False]


def test_always_on_reports_when_windows_startup_fails(tmp_path, monkeypatch, capsys) -> None:
    def boom(_):
        raise OSError("not windows")
    monkeypatch.setattr(autostart, "set_enabled", boom)
    ctx, _ = _ctx(tmp_path)
    assert main._always_on(ctx, True) == main.EXIT_OS_ERROR
    assert "not windows" in capsys.readouterr().out
