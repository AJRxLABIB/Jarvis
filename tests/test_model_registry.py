"""Model registry: one honest row per model; nothing the engine did not report is invented."""
import asyncio
from types import SimpleNamespace as NS

from app.ai.base import ModelInfo
from app.core import cognition as C, model_registry as MR


def test_rows_join_engine_metadata_speed_and_licence_without_inventing():
    models = [ModelInfo("jarvis:latest", 5_200_000_000, "qwen3", "4B", "Q4_K_M", None, frozenset({"completion", "tools"})),
              ModelInfo("mystery", 1_000_000_000, None, None, None, None, None),
              ModelInfo("qwen3-embedding:0.6b", 639_000_000, "qwen3", "0.6B", "Q8_0", None, frozenset({"embedding"}))]
    speed = {"jarvis": {"tokens_per_s": 42.0, "load_ms": 3100, "gpu_share": 0.8, "at": "2026-11-25T10:00:00"}}
    details = {"jarvis:latest": {"license": "Apache License 2.0", "context_length": 40960}}
    rows = {e.name: e for e in MR.build(models, speed, details)}
    j = rows["jarvis:latest"]
    assert j.tokens_per_s == 42.0 and j.load_seconds == 3.1 and j.license_head == "Apache License 2.0" and j.context_length == 40960
    assert any("only 80%" in n for n in j.notes)                         # a spill is said, not hidden
    m = rows["mystery"]
    assert m.capabilities is None and any("none assumed" in n for n in m.notes) and any("licence text not reported" in n for n in m.notes)
    assert rows["qwen3-embedding:0.6b"].role == "embedding"
    assert [e.role for e in MR.build(models)][-1] == "embedding"         # chat models first
    text = MR.report(MR.build(models, speed, details))
    assert "not reported" in text and "not measured" in text and "not legal advice" in text


def test_tool_reads_models_and_survives_missing_details(monkeypatch):
    from app.tools import provider_tools as PT

    class Fake:
        def __init__(self, *a, **k): ...
        async def list_models(self): return [ModelInfo("a:latest", 2_000_000_000, "x", "3B", "Q4", None, frozenset({"completion"}))]
        async def model_details(self, name): raise RuntimeError("no show")
        async def aclose(self): ...

    monkeypatch.setattr(PT, "OllamaProvider", Fake)
    ctx = NS(settings=NS(ollama_host="http://x", forced_offline=False), paths=None)
    r = asyncio.run(PT.model_registry(None, ctx))
    assert r.ok and "a:latest" in r.message and "licence text not reported" in r.message


def test_mode_names_match_the_five_levels():
    assert list(C.MODES.values()) == ["Quick", "Standard", "Advanced", "Expert", "Mission"]
    assert C.classify("what time is it").mode in ("Quick", "Standard")
    assert "mode - reasoning level" in C.classify("what time is it").text()
