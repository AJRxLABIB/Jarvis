"""3.1.5: fast answers - the model must fit on the GPU (measured, fitted, reported), Ollama started with
memory-saving settings, and the time of each answer split into load / read / write."""

from __future__ import annotations

import asyncio

from app.ai.base import ChatStats
from app.ai.manager import AIManager
from app.config.settings import Settings
from tests.fake_ollama import FakeOllama, model_entry


class GpuOllama(FakeOllama):
    """Reports how much of the model is in GPU memory, depending on the context size it was loaded with."""

    share_by_context: dict[int, float] = {}
    loaded_ctx = 0

    def handle(self, request):
        import json

        import httpx

        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path == "/api/generate" and body and body.get("keep_alive") != 0:
            self.loaded_ctx = (body.get("options") or {}).get("num_ctx", 0)
        if path == "/api/ps":
            self.requests.append((request.method, path, None))
            share = self.share_by_context.get(self.loaded_ctx, 1.0)
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b-instruct", "size": 4_000_000_000,
                                                          "size_vram": int(4_000_000_000 * share)}]})
        return super().handle(request)


def run(ai: AIManager, coro):
    async def go():
        try:
            return await coro
        finally:
            await ai.aclose()

    return asyncio.run(go())


def make(shares: dict[int, float], context: int = 6144):
    fake = GpuOllama(models=[model_entry("qwen3:4b-instruct")])
    fake.share_by_context = shares
    ai = AIManager.from_settings(Settings(ai_model="qwen3:4b-instruct"), transport=fake.transport())
    ai.provider.context_window = context
    return ai, fake


def test_a_model_that_fits_is_left_alone() -> None:
    ai, fake = make({6144: 1.0})
    run(ai, ai.warm_up())
    assert ai.gpu_share == 1.0 and ai.provider.context_window == 6144


def test_a_model_partly_on_the_cpu_gets_a_smaller_context_until_it_fits() -> None:
    ai, fake = make({8192: 0.6, 6144: 0.78, 4096: 1.0}, context=8192)
    run(ai, ai.warm_up())
    assert ai.provider.context_window == 4096 and ai.gpu_share == 1.0
    unloads = [b for _, p, b in fake.requests if p == "/api/generate" and b and b.get("keep_alive") == 0]
    assert len(unloads) == 2  # reloaded at 6144, then at 4096


def test_it_never_goes_below_the_smallest_step() -> None:
    ai, fake = make({6144: 0.6, 4096: 0.7})
    run(ai, ai.warm_up())
    assert ai.provider.context_window == 4096 and ai.gpu_share == 0.7


def test_a_cpu_only_pc_is_not_shrunk() -> None:
    ai, fake = make({6144: 0.0})
    run(ai, ai.warm_up())
    assert ai.provider.context_window == 6144


def test_answer_time_is_split() -> None:
    from app.ai.ollama import _parse_stats

    stats = _parse_stats({"prompt_eval_count": 2000, "eval_count": 40, "total_duration": 9_000_000_000,
                          "load_duration": 100_000_000, "prompt_eval_duration": 7_000_000_000,
                          "eval_duration": 1_800_000_000})
    assert stats == ChatStats(2000, 40, 9000, 100, 7000, 1800)


def test_ollama_is_started_with_memory_saving_settings(monkeypatch) -> None:
    from app.tools import winapi
    from app.utils import ollama_service

    seen = {}
    monkeypatch.setattr(winapi, "is_windows", lambda: True)
    monkeypatch.setattr(ollama_service, "ollama_running", lambda: False)
    monkeypatch.setattr(ollama_service, "find_ollama_server", lambda: "C:/Ollama/ollama.exe")
    monkeypatch.setattr(ollama_service, "find_ollama_app", lambda: None)
    monkeypatch.setattr(winapi, "start_program", lambda exe, args, **kw: seen.update(kw))
    monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)
    monkeypatch.setenv("OLLAMA_FLASH_ATTENTION", "0")  # one you set yourself wins
    assert ollama_service.start_ollama()
    assert seen["env"]["OLLAMA_KV_CACHE_TYPE"] == "q8_0" and seen["env"]["OLLAMA_FLASH_ATTENTION"] == "0"
    assert ollama_service.start_ollama(fast_memory=False)
    assert "OLLAMA_KV_CACHE_TYPE" not in seen["env"]


def test_health_report_tracks_the_gpu_share() -> None:
    from app import health

    lines = health.compare({"ai_on_gpu": 100}, {"ai_on_gpu": 78})
    assert lines == ["AI model on the GPU: 100%  (better than 78%)"]
