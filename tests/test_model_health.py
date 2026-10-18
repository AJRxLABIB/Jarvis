"""Model health: failing role models are skipped for a cool-off period, then retried; the default is never blocked."""

from __future__ import annotations

import asyncio

from app.ai.base import ChatMessage
from app.ai.health import BASE_COOLDOWN, FAILURES_TO_OPEN, MAX_COOLDOWN, ModelHealth
from app.ai.manager import AIManager
from app.config.settings import Settings
from tests.test_v3190 import Failing, models


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_breaker_opens_after_repeated_failures_and_closes_on_success() -> None:
    clock = Clock()
    health = ModelHealth(clock)
    for _ in range(FAILURES_TO_OPEN - 1):
        health.failure("m", "boom")
    assert health.available("m")  # not yet
    health.failure("m", "boom")
    assert not health.available("m") and health.cooldown_left("m") == BASE_COOLDOWN
    clock.t += BASE_COOLDOWN + 1
    assert health.available("m")  # cool-off over: it is tried again
    health.failure("m", "boom")  # still failing: the next cool-off is longer
    assert health.cooldown_left("m") == BASE_COOLDOWN * 2
    health.success("m", 0.5)
    assert health.available("m") and health.summary()["m"]["avg_seconds"] == 0.5


def test_cooldown_is_capped() -> None:
    health = ModelHealth(Clock())
    for _ in range(30):
        health.failure("m", "boom")
    assert health.cooldown_left("m") == MAX_COOLDOWN


def test_unknown_model_is_available() -> None:
    assert ModelHealth().available("never-seen")


def test_failing_smart_model_is_skipped_after_repeated_failures() -> None:
    fake = Failing(models=models("qwen3:4b-instruct", "qwen3:8b"))
    fake.broken = "qwen3:8b"
    ai = AIManager.from_settings(Settings(ai_model="qwen3:4b-instruct", ai_smart_model="qwen3:8b"),
                                 transport=fake.transport())
    for _ in range(FAILURES_TO_OPEN):  # each request tries the smart model, fails, and falls back once
        assert asyncio.run(ai.chat([ChatMessage.user("explain it")], smart=True)).model == "qwen3:4b-instruct"
    assert not ai.health.available("qwen3:8b")
    attempts = len(fake.requests) if hasattr(fake, "requests") else None
    response = asyncio.run(ai.chat([ChatMessage.user("explain it")], smart=True))
    assert response.model == "qwen3:4b-instruct"                                      # not even tried ...
    assert "qwen3:8b kept failing" in (ai.last_route["fallback"] or "")                # ... and you are told why
    if attempts is not None:
        assert len(fake.requests) == attempts + 1  # one request, not two
    assert ai.health.summary()["qwen3:8b"]["failures"] == FAILURES_TO_OPEN


def test_default_model_is_never_blocked() -> None:
    fake = Failing(models=models("qwen3:4b-instruct"))
    ai = AIManager.from_settings(Settings(ai_model="qwen3:4b-instruct"), transport=fake.transport())
    for _ in range(5):
        ai.health.failure("qwen3:4b-instruct", "boom")
    assert asyncio.run(ai.chat([ChatMessage.user("hello")])).model == "qwen3:4b-instruct"


def test_could_ever_fit() -> None:
    from app.ai.health import could_ever_fit

    gb = 1024**3
    assert could_ever_fit(4 * gb, 16 * gb) and could_ever_fit(None, 16 * gb) and could_ever_fit(4 * gb, None)
    assert not could_ever_fit(15 * gb, 16 * gb)  # with headroom it can't load
    assert not could_ever_fit(40 * gb, 16 * gb)


def test_a_model_bigger_than_the_ram_is_not_tried(monkeypatch) -> None:
    from app.ai import manager as manager_module

    fake = Failing(models=models("qwen3:4b-instruct", "qwen3:8b"))
    ai = AIManager.from_settings(Settings(ai_model="qwen3:4b-instruct", ai_smart_model="qwen3:8b"),
                                 transport=fake.transport())
    monkeypatch.setattr(manager_module, "total_memory_bytes", lambda: 1)  # a tiny PC: nothing big fits
    response = asyncio.run(ai.chat([ChatMessage.user("explain it")], smart=True))
    assert response.model == "qwen3:4b-instruct" and ai.last_route["role"] == "default"


def test_the_user_is_told_when_a_fallback_model_answered() -> None:
    from app.agent.conversation import ConversationManager
    from datetime import datetime, timezone

    fake = Failing(models=models("qwen3:4b-instruct", "qwen3:8b"))
    fake.broken = "qwen3:8b"
    settings = Settings(ai_model="qwen3:4b-instruct", ai_smart_model="qwen3:8b")
    conv = ConversationManager(AIManager.from_settings(settings, transport=fake.transport()), settings, None,
                               clock=lambda: datetime(2026, 9, 23, 14, 30, tzinfo=timezone.utc))
    pieces: list[str] = []

    async def go():
        try:
            return await conv.send("explain the difference between TCP and UDP in detail", pieces.append)
        finally:
            await conv.ai.aclose()

    reply = asyncio.run(go())
    assert reply.ok and "qwen3:8b failed" in reply.text and "I answered with qwen3:4b-instruct" in reply.text
    assert "less detailed" in "".join(pieces)  # also shown while streaming


def test_no_notice_when_the_planned_model_answered() -> None:
    from app.agent.conversation import ConversationManager
    from datetime import datetime, timezone

    fake = Failing(models=models("qwen3:4b-instruct"))
    settings = Settings(ai_model="qwen3:4b-instruct")
    conv = ConversationManager(AIManager.from_settings(settings, transport=fake.transport()), settings, None,
                               clock=lambda: datetime(2026, 9, 23, 14, 30, tzinfo=timezone.utc))

    async def go():
        try:
            return await conv.send("hello there")
        finally:
            await conv.ai.aclose()

    assert "Note:" not in asyncio.run(go()).text
