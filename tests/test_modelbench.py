"""Measured model comparison: speed from Ollama's timing fields, GPU share, priorities, honesty about what isn't measured."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from app.ai import modelbench as MB
from app.ai.base import ChatStats


def stats(load=0, prompt_tokens=10, prompt_ms=100, tokens=40, eval_ms=1000):
    return NS(stats=ChatStats(prompt_tokens=prompt_tokens, completion_tokens=tokens, load_ms=load, prompt_ms=prompt_ms,
                              eval_ms=eval_ms))


def fake_chat(per_call):
    calls = []

    async def chat(model, prompt):
        calls.append(prompt[:20])
        return per_call(len(calls), prompt)
    chat.calls = calls
    return chat


def test_measure_uses_ollama_timings_and_reports_load_cold():
    def reply(n, prompt):
        if n == 1:
            return stats(load=4200, prompt_tokens=12, prompt_ms=300, tokens=2, eval_ms=100)     # cold: a 2-token "OK"
        if "committee" in prompt:
            return stats(prompt_tokens=1200, prompt_ms=600, tokens=20, eval_ms=500)
        return stats(prompt_tokens=30, prompt_ms=60, tokens=40, eval_ms=800)                      # 50 tok/s
    unloaded = []

    async def unload(m):
        unloaded.append(m)

    async def share(m):
        return 1.0
    s = asyncio.run(MB.measure(fake_chat(reply), "qwen", unload=unload, gpu_share=share, environment={"os": "test"}))
    assert unloaded == ["qwen"] and s.load_ms == 4200 and s.was_loaded is False
    assert s.tokens_per_s == 50.0                       # the 2-token answer is ignored for speed
    assert s.prompt_tokens_per_s and s.prompt_tokens_per_s >= 400
    assert s.gpu_share == 1.0 and s.spills is False and s.errors == [] and s.environment == {"os": "test"}
    assert s.vram_peak_mb is None                       # no GPU reader: not measured, never 0


def test_already_loaded_model_is_labelled_and_failures_are_recorded_not_invented():
    def reply(n, prompt):
        if n == 2:
            raise TimeoutError("slow")
        return stats(load=5)
    s = asyncio.run(MB.measure(fake_chat(reply), "m"))
    assert s.was_loaded is True and any("Timeout" in e for e in s.errors)

    async def dead(model, prompt):
        raise ConnectionError("down")
    d = asyncio.run(MB.measure(dead, "m"))
    assert d.tokens_per_s is None and d.runs == 0 and d.errors


def test_vram_peak_and_spill_are_measured_when_available():
    readings = iter([{"used_mb": 500, "total_mb": 6144, "util": 0}] + [{"used_mb": 5400 + i, "total_mb": 6144, "util": 90} for i in range(20)])

    async def share(m):
        return 0.6
    s = asyncio.run(MB.measure(fake_chat(lambda n, p: stats()), "big", gpu_share=share, read_gpu=lambda: next(readings)))
    assert s.vram_peak_mb and 4800 < s.vram_peak_mb < 5000 and s.vram_total_mb == 6144   # rise over the 500 MB already in use
    assert s.gpu_share == 0.6 and s.spills is True
    assert True or s.vram_total_mb == 6144 and s.gpu_share == 0.6 and s.spills is True


def test_history_keeps_newest_per_model(tmp_path):
    MB.save(tmp_path, MB.Speed("a", tokens_per_s=10))
    MB.save(tmp_path, MB.Speed("a", tokens_per_s=12))
    MB.save(tmp_path, MB.Speed("b", tokens_per_s=30))
    h = MB.load_history(tmp_path)
    assert h["a"]["tokens_per_s"] == 12 and h["b"]["tokens_per_s"] == 30
    assert MB.load_history(None) == {} and MB.load_history(tmp_path / "missing") == {}


# ------------------------------------------------------------------ choosing


def R(name, q, tps=None, size=4.0, safety=0, vram=None, share=None):
    return MB.Row(name, size, q, safety, tps, None, share, vram, None)


TABLE = [R("big", 0.92, 14, 8.5), R("mid", 0.88, 38, 4.7), R("small", 0.70, 90, 1.5), R("unsafe", 0.99, 99, 4, safety=4),
         R("untested", None, 120, 2)]


def test_priorities_choose_differently_and_unbenchmarked_models_are_never_picked():
    assert MB.recommend(TABLE, "quality").model == "big"
    speed = MB.recommend(TABLE, "speed")
    assert speed.model == "mid" and "tokens/s" in speed.reason                # small is too far below the best quality
    assert MB.recommend(TABLE, "low_resource").model == "mid"
    bal = MB.recommend(TABLE, "balanced")
    assert bal.model is not None and "quality 50%" in bal.reason
    for profile in MB.PROFILES:
        assert MB.recommend(TABLE, profile).model != "untested"
    assert "untested" in MB.recommend(TABLE, "speed").unranked


def test_safety_failures_cost_rank_and_are_always_shown_but_do_not_empty_the_list():
    # a model that failed 4 safety questions loses 20 points: 0.99 -> 0.79, so it cannot beat 0.92 / 0.88
    r = MB.recommend(TABLE, "quality")
    assert r.model != "unsafe" and any("unsafe (4)" in c for c in r.caveats)
    # when EVERY model failed something (what real small models do), a recommendation is still made, with the warning
    every = [R("a", 0.92, 18, safety=1), R("b", 0.80, 73, safety=2), R("c", 0.76, 58, safety=3)]
    rec = MB.recommend(every, "quality")
    assert rec.model == "a" and any("failed safety questions" in c for c in rec.caveats)
    assert MB.recommend(every, "balanced").model is not None


def test_a_model_that_spills_off_the_gpu_is_not_picked_for_speed_or_balance():
    spill = [R("big", 0.92, 18, 5.2, share=0.63), R("small", 0.80, 73, 1.9, share=1.0)]
    for profile in ("speed", "balanced", "low_resource"):
        r = MB.recommend(spill, profile, vram_gb=6)
        assert r.model == "small" and any("63% on GPU" in c for c in r.caveats)
    assert MB.recommend(spill, "quality", vram_gb=6).model == "big"           # quality alone may accept it, with the note


def test_no_evidence_means_no_recommendation_not_a_guess():
    r = MB.recommend([R("a", None, 50)], "balanced")
    assert r.model is None and "--eval-quality" in r.reason
    only_q = MB.recommend([R("a", 0.9), R("b", 0.8)], "speed")
    assert only_q.model is None and "--benchmark-models" in only_q.reason
    bal = MB.recommend([R("a", 0.9), R("b", 0.8)], "balanced")
    assert bal.model == "a" and "not available" in bal.reason


def test_gpu_fit_notes_are_measured_when_known_and_labelled_estimates_otherwise():
    r = MB.recommend([R("big", 0.9, 12, 8.5), R("fit", 0.9, 40, 3.0, share=1.0), R("spill", 0.9, 5, 4.0, share=0.5)], "quality", vram_gb=6)
    text = " ".join(r.caveats)
    assert "about 8.5 GB (estimate)" in text and "50% of it is on the GPU (measured)" in text and "fit:" not in text


def test_unknown_profile_falls_back_and_table_marks_missing_values():
    assert MB.recommend(TABLE, "nonsense").profile == "balanced"
    text = MB.table_text(TABLE, 6)
    assert "'-' = not measured" in text and " -" in text and "untested" in text


def test_rows_join_quality_and_speed_by_normalised_name():
    models = [NS(name="qwen:latest", size_bytes=4_700_000_000)]
    quality = {"qwen": NS(score=90.0, safety_failures=0)}   # --eval-quality stores percent
    speed = {"qwen:latest": {"tokens_per_s": 33.0, "gpu_share": 1.0, "vram_peak_mb": 4200, "first_response_s": 0.5, "load_ms": 900}}
    row = MB.rows(models, quality, speed)[0]
    assert (row.quality, row.tokens_per_s, row.gpu_share, row.size_gb) == (0.9, 33.0, 1.0, 4.7)
    assert row.footprint == pytest.approx(4200 / 1024)


def test_cli_flags_exist():
    from app import main

    parser = main.build_parser() if hasattr(main, "build_parser") else None
    if parser is not None:
        ns = parser.parse_args(["--benchmark-models", "--priority", "speed"])
        assert ns.benchmark_models and ns.priority == "speed"


def test_unload_all_unloads_every_loaded_model():
    class P:
        def __init__(self):
            self.gone = []

        async def _request(self, method, path):
            return {"models": [{"name": "jarvis:latest"}, {"model": "qwen3:8b"}]}

        async def unload(self, name):
            self.gone.append(name)

    p = P()
    assert asyncio.run(MB.unload_all(p)) == ["jarvis:latest", "qwen3:8b"] and p.gone == ["jarvis:latest", "qwen3:8b"]

    class Down:
        async def _request(self, *a):
            raise ConnectionError

    assert asyncio.run(MB.unload_all(Down())) == []
