from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.ai.base import ChatMessage, ModelCapabilityError, ModelNotFoundError, NoModelAvailableError, ToolSpec
from app.ai.manager import SAVED_MODEL_KEY, AIManager, find_model
from app.config.settings import Settings
from app.memory.database import Database
from app.memory.settings_store import SettingsStore
from tests.fake_ollama import FakeOllama, model_entry


@pytest.fixture()
def store(tmp_path: Path) -> SettingsStore:
    db = Database(tmp_path / "a.db")
    db.initialize()
    return SettingsStore(db)


def manager(fake: FakeOllama, store: SettingsStore | None = None, **settings) -> AIManager:
    return AIManager.from_settings(Settings(**settings), store, transport=fake.transport())


def run(ai: AIManager, coro_fn):
    async def _inner():
        try:
            return await coro_fn(ai)
        finally:
            await ai.aclose()

    return asyncio.run(_inner())


def fake_with_models() -> FakeOllama:
    return FakeOllama(
        models=[
            model_entry("old-tools:latest", "2025-01-01T00:00:00Z"),
            model_entry("new-notools:latest", "2026-06-01T00:00:00Z"),
            model_entry("new-tools:7b", "2026-05-01T00:00:00Z"),
            model_entry("nomic-embed-text:latest", "2026-07-01T00:00:00Z", family="nomic-bert"),
        ],
        capabilities={
            "new-notools:latest": ["completion"],
            "nomic-embed-text:latest": ["embedding"],
        },
    )


def test_auto_selects_newest_tool_capable_chat_model() -> None:
    model, source = run(manager(fake_with_models()), lambda ai: ai.resolve_model())
    assert (model.name, source) == ("new-tools:7b", "auto")


def test_env_model_wins_and_latest_suffix_optional() -> None:
    model, source = run(manager(fake_with_models(), ai_model="old-tools"), lambda ai: ai.resolve_model())
    assert (model.name, source) == ("old-tools:latest", "env")


def test_env_model_missing_is_an_error_not_a_silent_fallback() -> None:
    with pytest.raises(ModelNotFoundError, match="AI_MODEL"):
        run(manager(fake_with_models(), ai_model="ghost"), lambda ai: ai.resolve_model())


def test_no_chat_models() -> None:
    fake = FakeOllama(models=[model_entry("nomic-embed-text")], capabilities={"nomic-embed-text": ["embedding"]})
    with pytest.raises(NoModelAvailableError):
        run(manager(fake), lambda ai: ai.resolve_model())


def test_set_model_persists_and_is_used_next_time(store: SettingsStore) -> None:
    model = run(manager(fake_with_models(), store), lambda ai: ai.set_model("OLD-TOOLS"))
    assert model.name == "old-tools:latest"
    assert store.get(SAVED_MODEL_KEY) == "old-tools:latest"
    _, source = run(manager(fake_with_models(), store), lambda ai: ai.resolve_model())
    assert source == "saved"


def test_saved_model_that_was_removed_falls_back_to_auto(store: SettingsStore) -> None:
    store.set(SAVED_MODEL_KEY, "deleted-model:latest")
    model, source = run(manager(fake_with_models(), store), lambda ai: ai.resolve_model())
    assert (model.name, source) == ("new-tools:7b", "auto")


def test_set_model_rejects_embedding_and_unknown() -> None:
    with pytest.raises(ModelCapabilityError):
        run(manager(fake_with_models()), lambda ai: ai.set_model("nomic-embed-text"))
    with pytest.raises(ModelNotFoundError):
        run(manager(fake_with_models()), lambda ai: ai.set_model("ghost"))


def test_tools_refused_for_model_without_tool_support() -> None:
    tool = ToolSpec("t", "d")
    with pytest.raises(ModelCapabilityError):
        run(manager(fake_with_models(), ai_model="new-notools"), lambda ai: ai.chat([ChatMessage.user("x")], [tool]))


def test_think_only_sent_to_thinking_models() -> None:
    fake = FakeOllama(models=[model_entry("qwen3:8b")], capabilities={"qwen3:8b": ["completion", "tools", "thinking"]})
    run(manager(fake), lambda ai: ai.chat([ChatMessage.user("x")]))
    assert fake.last_chat_payload()["think"] is False


def test_status_when_ollama_down() -> None:
    fake = FakeOllama(raise_on={"/api/version": httpx.ConnectError("refused")})
    status = run(manager(fake), lambda ai: ai.status())
    assert not status.reachable and "not running" in status.message


def test_status_ok() -> None:
    status = run(manager(fake_with_models()), lambda ai: ai.status())
    assert status.reachable and status.version == "0.12.0" and status.selected.name == "new-tools:7b"
    assert len(status.models) == 4


def test_find_model() -> None:
    from app.ai.base import ModelInfo

    models = [ModelInfo("Llama3.2:latest"), ModelInfo("qwen3:8b")]
    assert find_model(models, "llama3.2").name == "Llama3.2:latest"
    assert find_model(models, "qwen3").name == "qwen3:8b"  # only one qwen3 installed
    assert find_model(models + [ModelInfo("qwen3:32b")], "qwen3") is None  # ambiguous
    assert find_model([], "x") is None
