from __future__ import annotations

import io
from pathlib import Path

import pytest

from app import cli
from app.ai.manager import AIManager
from app.main import bootstrap
from app.memory.settings_store import SettingsStore
from tests.fake_ollama import FakeOllama, model_entry


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOllama:
    f = FakeOllama(models=[model_entry("llama3.2:latest"), model_entry("qwen3:8b", "2020-01-01T00:00:00Z")])
    monkeypatch.setattr(
        cli, "create_ai_manager",
        lambda ctx: AIManager.from_settings(ctx.settings, SettingsStore(ctx.database), transport=f.transport()),
    )
    return f


def feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    it = iter(lines)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_chat_session(tmp_path: Path, fake: FakeOllama, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = bootstrap(tmp_path, environ={})
    feed(monkeypatch, ["/help", "Could you check the clock for me?", "/model qwen3", "/model ghost", "/reset", "/bogus", "/exit"])
    out = io.StringIO()
    assert cli.run_chat(ctx, out) == 0
    text = out.getvalue()
    assert "Using: llama3.2:latest" in text
    assert "Jarvis: Hello." in text
    assert "Now using qwen3:8b." in text
    assert "'ghost' is not installed" in text
    assert "Conversation cleared." in text and "Unknown command" in text
    assert SettingsStore(ctx.database).get("ai.model") == "qwen3:8b"


def test_chat_exits_on_eof(tmp_path: Path, fake: FakeOllama, monkeypatch: pytest.MonkeyPatch) -> None:
    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert cli.run_chat(bootstrap(tmp_path, environ={}), io.StringIO()) == 0


def test_models_command(tmp_path: Path, fake: FakeOllama) -> None:
    out = io.StringIO()
    assert cli.run_models(bootstrap(tmp_path, environ={}), out) == 0
    assert "* llama3.2:latest" in out.getvalue() and "tools: yes" in out.getvalue()
