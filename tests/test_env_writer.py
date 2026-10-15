from __future__ import annotations

from pathlib import Path

import pytest

from app.config.env_writer import format_env_value, render_env, update_env_file
from app.config.settings import ConfigError, load_settings


def test_format_values() -> None:
    assert format_env_value("llama3.2:3b") == "llama3.2:3b"
    assert format_env_value("") == ""
    assert format_env_value("D:\\My Projects\\App") == "'D:\\My Projects\\App'"
    assert format_env_value("Microphone Array (Realtek)") == "'Microphone Array (Realtek)'"
    assert format_env_value("it's") == '"it\'s"'
    with pytest.raises(ConfigError):
        format_env_value("a\nEVIL=1")


def test_render_keeps_comments_and_order() -> None:
    text = "# my notes\nAI_MODEL=old\n\n# voice\nTTS_SPEED=1.0\nCUSTOM_THING=keep\n"
    out = render_env(text, {"AI_MODEL": "qwen3:8b", "USER_ADDRESS": "Labib"})
    assert out.splitlines()[:6] == ["# my notes", "AI_MODEL=qwen3:8b", "", "# voice", "TTS_SPEED=1.0", "CUSTOM_THING=keep"]
    assert out.rstrip().endswith("USER_ADDRESS=Labib")


def test_round_trip_through_the_real_loader(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# settings\nAI_MODEL=\n", encoding="utf-8")
    values = {"AI_MODEL": "llama3.2:3b", "MICROPHONE_DEVICE": "Microphone Array (Realtek(R) Audio)",
              "DEV_PROJECT_DIR": str(tmp_path / "My Projects"), "TTS_SPEED": "1.2", "TTS_ENABLED": "false"}
    settings = update_env_file(env, values)
    reloaded = load_settings(env, environ={})
    assert reloaded == settings
    assert reloaded.microphone_device == "Microphone Array (Realtek(R) Audio)"
    assert str(reloaded.dev_project_dir) == str(tmp_path / "My Projects")
    assert reloaded.tts_speed == 1.2 and reloaded.tts_enabled is False
    assert env.read_text().startswith("# settings\n")


def test_windows_path_with_backslashes_survives(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    update_env_file(env, {"STT_MODEL": "base.en", "TTS_SAPI_VOICE": "Microsoft Zira\\Desktop"})
    assert load_settings(env, environ={}).tts_sapi_voice == "Microsoft Zira\\Desktop"


def test_invalid_values_leave_the_file_untouched(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TTS_SPEED=1.0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="TTS_SPEED"):
        update_env_file(env, {"TTS_SPEED": "9"})
    assert env.read_text() == "TTS_SPEED=1.0\n"
    assert not list(tmp_path.glob(".env.*.tmp"))
