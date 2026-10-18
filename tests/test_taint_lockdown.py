from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.agent.conversation import ConversationManager
from app.ai.base import ChatMessage, ToolSpec
from app.config.settings import Settings


class SpyAI:
    def __init__(self) -> None:
        self.offered: list[str] = []

    async def chat(self, messages, tools, **_):
        self.offered = [t.name for t in tools]
        return SimpleNamespace(model="m", message=ChatMessage.assistant("ok"), stats=None)


def _conv(lockdown: bool, tainted: bool) -> tuple[ConversationManager, SpyAI]:
    spy = SpyAI()
    conv = ConversationManager.__new__(ConversationManager)
    conv.ai, conv.settings = spy, Settings(taint_lockdown=lockdown)
    conv.taint = SimpleNamespace(active=tainted)
    conv._route = lambda: SimpleNamespace(kind="normal", sensitive=False, reason="")
    return conv, spy


TOOLS = [ToolSpec(n, "d") for n in ("send_email", "web_search", "remember_fact", "get_weather", "browser_type")]


def _offered(lockdown: bool, tainted: bool) -> list[str]:
    conv, spy = _conv(lockdown, tainted)
    asyncio.run(conv._complete([ChatMessage.user("hi")], TOOLS, None, {t.name for t in TOOLS}))
    return spy.offered


def test_lockdown_withholds_sending_tools_after_untrusted_text() -> None:
    offered = _offered(True, True)
    assert "send_email" not in offered and "browser_type" not in offered
    assert "web_search" in offered and "get_weather" in offered


def test_lockdown_off_or_clean_conversation_offers_everything() -> None:
    assert len(_offered(False, True)) == len(TOOLS)
    assert len(_offered(True, False)) == len(TOOLS)


def test_mic_watch_warns_once_after_sustained_silence_and_resets_on_sound() -> None:
    from app.voice.hands_free import MicWatch

    w = MicWatch(seconds=10, floor_db=-65)
    assert not any(w.feed(-90, 1.0) for _ in range(9))
    assert w.feed(-90, 1.0) is True and w.feed(-90, 1.0) is False        # once
    w2 = MicWatch(seconds=10, floor_db=-65)
    for _ in range(9):
        w2.feed(-90, 1.0)
    assert w2.feed(-40, 1.0) is False                                     # a voice resets the count
    assert not any(w2.feed(-90, 1.0) for _ in range(9))
