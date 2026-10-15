from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import ConfigError, Settings, load_settings


def test_defaults_without_env_file(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / ".env", environ={})
    assert settings == Settings()
    assert settings.ai_provider == "ollama"
    assert settings.ai_model == ""
    assert settings.data_dir is None


def test_env_file_is_read_and_bom_tolerated(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("\ufeffAI_MODEL=llama3.1:8b\nTTS_ENABLED=false\nWAKE_WORD=  Hey   Nova \n", encoding="utf-8")
    settings = load_settings(env, environ={})
    assert settings.ai_model == "llama3.1:8b"
    assert settings.tts_enabled is False
    assert settings.wake_word == "hey nova"


def test_environment_overrides_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("LOG_LEVEL=warning\n", encoding="utf-8")
    settings = load_settings(env, environ={"LOG_LEVEL": "error"})
    assert settings.log_level == "ERROR"


def test_debug_forces_debug_log_level() -> None:
    assert Settings(debug=True, log_level="ERROR").effective_log_level == "DEBUG"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AI_PROVIDER", "skynet"),
        ("OLLAMA_HOST", "127.0.0.1:11434"),
        ("OLLAMA_HOST", "http://user:pw@localhost:11434"),
        ("OLLAMA_HOST", "http://localhost:11434/api"),
        ("AI_MODEL", "llama; rm -rf"),
        ("WAKE_WORD", "hey 123"),
        ("VOICE_LANGUAGE", "english!"),
        ("LOG_LEVEL", "LOUD"),
        ("TTS_ENABLED", "maybe"),
        ("JARVIS_DATA_DIR", "relative\\folder"),
    ],
)
def test_invalid_values_raise_readable_error(tmp_path: Path, name: str, value: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_settings(tmp_path / ".env", environ={name: value})
    assert name in str(info.value)


def test_host_trailing_slash_removed() -> None:
    assert Settings(ollama_host="http://localhost:11434/").ollama_host == "http://localhost:11434"


def test_empty_data_dir_is_none_and_absolute_accepted(tmp_path: Path) -> None:
    assert load_settings(None, environ={"JARVIS_DATA_DIR": ""}).data_dir is None
    assert load_settings(None, environ={"JARVIS_DATA_DIR": str(tmp_path)}).data_dir == tmp_path


def test_settings_are_immutable() -> None:
    settings = Settings()
    with pytest.raises(Exception):
        settings.ai_model = "x"  # type: ignore[misc]


def test_phase2_settings() -> None:
    s = load_settings(None, environ={"USER_ADDRESS": "Labib", "OLLAMA_KEEP_ALIVE": "-1", "AI_THINK": "true", "AI_CONTEXT_WINDOW": "16384"})
    assert (s.user_address, s.ollama_keep_alive, s.ai_think, s.ai_context_window) == ("Labib", "-1", True, 16384)
    for name, value in (("USER_ADDRESS", "<script>"), ("OLLAMA_KEEP_ALIVE", "forever"), ("AI_CONTEXT_WINDOW", "10")):
        with pytest.raises(ConfigError, match=name):
            load_settings(None, environ={name: value})


def test_unparseable_env_line_is_reported_with_line_number(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('AI_MODEL=llama3.2\n\n  $env:MICROPHONE_DEVICE="Microphone Array"; python run.py --mic-test\n', encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_settings(env, environ={})
    message = str(info.value)
    assert "line 3" in message and "MICROPHONE_DEVICE" in message and "NAME=value" in message


def test_env_values_with_spaces_and_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nMICROPHONE_DEVICE=Microphone Array\nTTS_SAPI_VOICE=\"Zira\"  # inline\n", encoding="utf-8")
    s = load_settings(env, environ={})
    assert s.microphone_device == "Microphone Array" and s.tts_sapi_voice == "Zira"
