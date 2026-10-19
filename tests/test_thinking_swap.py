"""Hard questions use a bigger installed model only when it fits entirely on the GPU."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.ai import thinking_swap as ts
from app.ai.base import ChatMessage, ModelInfo
from app.ai.manager import AIManager
from app.config.settings import Settings
from tests.test_v3190 import Failing, models

GB = 10 ** 9


def info(name, gb, caps=("completion", "tools")):
    return ModelInfo(name, int(gb * GB), capabilities=frozenset(caps))


DEFAULT = info("small", 2.5)


def test_picks_the_largest_that_fits_preferring_thinking():
    installed = [DEFAULT, info("mid", 4.0), info("big", 5.0), info("think", 3.8, ("completion", "tools", "thinking"))]
    assert ts.pick(DEFAULT, installed, 6144 - 1024, tools=True).name == "think"      # 5 GB x 1.25 > 5.1 GB budget
    assert ts.pick(DEFAULT, installed[:3], 8192, tools=True).name == "big"
    assert ts.pick(DEFAULT, installed[:3], 5120, tools=True).name == "mid"


def test_never_swaps_when_it_would_not_fit_or_gpu_unknown_or_unusable():
    assert ts.pick(DEFAULT, [DEFAULT, info("big", 5.0)], 4096, tools=True) is None            # spills onto the CPU
    assert ts.pick(DEFAULT, [DEFAULT, info("mid", 4.0)], None, tools=True) is None            # can't prove it fits
    assert ts.pick(DEFAULT, [DEFAULT, info("notools", 4.0, ("completion",))], 8192, tools=True) is None
    assert ts.pick(DEFAULT, [DEFAULT, info("notools", 4.0, ("completion",))], 8192, tools=False).name == "notools"
    assert ts.pick(DEFAULT, [DEFAULT, info("embed", 4.0, ("embedding",))], 8192, tools=True) is None
    assert ts.pick(DEFAULT, [DEFAULT, info("mid", 4.0)], 8192, tools=True, resting=["mid"]) is None
    assert ts.pick(DEFAULT, [DEFAULT, info("tiny", 1.0)], 8192, tools=True) is None           # only bigger ones
    assert ts.gpu_budget_mb(6144, True) == 5120 and ts.gpu_budget_mb(24576, True) == 24576 and ts.gpu_budget_mb(0, True) is None


def test_the_manager_uses_it_for_hard_requests_only_and_says_so(tmp_path: Path, monkeypatch):
    from app.ai import manager as manager_module

    monkeypatch.setattr(manager_module, "total_memory_bytes", lambda: 64 * GB)
    entries = models("qwen3:4b-instruct", "qwen3:8b")
    entries[1]["size"] = 5_000_000_000
    fake = Failing(models=entries)
    manager = AIManager.from_settings(Settings(ai_model="qwen3:4b-instruct", data_dir=tmp_path), transport=fake.transport())
    sizes = {m.name: m.size_bytes for m in asyncio.run(manager.models())}

    async def total():
        return 12288.0

    manager._gpu_total_mb = total
    big = "qwen3:8b"
    assert sizes[big] > sizes["qwen3:4b-instruct"]
    assert asyncio.run(manager.chat([ChatMessage.user("hi")])).model == "qwen3:4b-instruct"
    smart = asyncio.run(manager.chat([ChatMessage.user("explain it")], smart=True))
    assert smart.model == big and manager.last_route["role"] == "smart-auto" and "bigger model" in manager.last_route["auto"]
    off = AIManager.from_settings(Settings(ai_model="qwen3:4b-instruct", ai_auto_smart=False, data_dir=tmp_path),
                                  transport=fake.transport())
    assert asyncio.run(off.chat([ChatMessage.user("explain it")], smart=True)).model == "qwen3:4b-instruct"
