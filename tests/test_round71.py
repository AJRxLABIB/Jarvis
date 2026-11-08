"""Round 71: standard verification states, resource orchestration (GPU fit, loaded models, workload scheduler), Job Object probe."""
from __future__ import annotations

import asyncio

import pytest

from app.core import resources as R


# ---- verification states ---------------------------------------------------------------------------------------------------------------

def _executor(tools):
    from app.agent.tool_manager import ToolExecutor, ToolRegistry
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine
    from app.tools.base import ToolContext
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return ToolExecutor(reg, PermissionEngine(PermissionsConfig()), ToolContext(Settings()))


def test_verification_states_through_the_real_executor():
    pytest.importorskip("pydantic")
    from app.ai.base import ToolCall
    from app.security.permissions import RiskLevel
    from app.tools.base import NoArgs, Tool, ToolResult
    ok = lambda a, c: ToolResult.success("done")
    tools = [Tool("read_only", "r", ok, NoArgs, RiskLevel.SAFE),
             Tool("changes", "c", ok, NoArgs, RiskLevel.MODERATE),
             Tool("proves", "p", ok, NoArgs, RiskLevel.SAFE, verify=lambda a, r, c: (True, "found")),
             Tool("lies", "l", ok, NoArgs, RiskLevel.SAFE, verify=lambda a, r, c: (False, "nothing there")),
             Tool("cannot", "x", ok, NoArgs, RiskLevel.SAFE, verify=lambda a, r, c: 1 / 0)]
    ex = _executor(tools)
    got = {}
    for name in ("read_only", "proves", "lies", "cannot"):
        r = asyncio.run(ex.execute(ToolCall(name, {}), user_command=True)).result
        got[name] = (r.ok, r.data.get("verification"))
    assert got == {"read_only": (True, "NOT_REQUIRED"), "proves": (True, "VERIFIED"), "lies": (False, "FAILED"), "cannot": (True, "UNCERTAIN")}


def test_coverage_report_lists_unverifiable_tools():
    from app.security.permissions import RiskLevel
    from app.tools.base import NoArgs, Tool, ToolResult
    from app.tools.verification_tool import coverage
    h = lambda a, c: ToolResult.success("x")
    c = coverage([Tool("a", "d", h, NoArgs, RiskLevel.SAFE), Tool("b", "d", h, NoArgs, RiskLevel.MODERATE, category="files"),
                  Tool("c", "d", h, NoArgs, RiskLevel.DANGEROUS, verify=lambda *a: (True, "")), Tool("d", "d", h, NoArgs, RiskLevel.SAFE, risk=lambda a, c: RiskLevel.DANGEROUS)])
    assert c["not_required"] == 1 and c["verified"] == ["c"] and c["unsupported"] == {"files": ["b"], "general": ["d"]}


# ---- GPU fit ---------------------------------------------------------------------------------------------------------------------------

GPU6 = R.Gpu("RTX 4050", 6.0, 1.0, 10.0, 50.0, None, "nvidia-smi")        # 5 GB free


def test_plan_load_uses_observed_memory_and_margin():
    assert R.plan_load(2.5, GPU6, 20.0).verdict == "gpu"                      # 2.5 x 1.15 + 0.6 = 3.5 < 5
    assert R.plan_load(4.5, GPU6, 20.0).verdict == "cpu"                      # 5.2 + 0.6 > 5 GB free -> RAM
    assert R.plan_load(4.5, GPU6, 20.0, observed_gb=3.0).verdict == "gpu"     # measured 3 GB beats the file-size estimate
    assert R.plan_load(30, GPU6, 8.0).verdict == "no" and R.plan_load(None, GPU6, 8.0).verdict == "unknown"
    cpu_only = R.plan_load(2.0, R.Gpu(), 16.0)
    assert cpu_only.verdict == "cpu" and "no GPU memory was read" in cpu_only.why
    assert R.plan_load(4.0, GPU6, 20.0, margin_gb=2.0).verdict == "cpu"       # a configurable margin


def test_read_gpu_falls_through_readers_and_never_requires_nvidia():
    assert R.read_gpu((lambda: None, lambda: None)) == R.Gpu() and R.Gpu().free_gb is None
    assert R.read_gpu((lambda: None, lambda: GPU6)).source == "nvidia-smi" and GPU6.free_gb == 5.0


def test_parse_ps_book_and_idle_rules():
    loaded = R.parse_ps({"models": [{"name": "qwen3:4b", "size": 3 * 2**30, "size_vram": 2 * 2**30, "expires_at": "x"}, {"name": "emb", "size": 2**29, "size_vram": 2**29}, {"bad": 1}]})
    assert [m.name for m in loaded][:2] == ["qwen3:4b", "emb"] and round(loaded[0].gpu_share, 2) == 0.67 and R.parse_ps(None) == []

    class KV:
        d: dict = {}
        def get(self, k, default=None): return self.d.get(k, default)
        def set(self, k, v): self.d[k] = v
    book = R.ModelBook(KV(), clock=lambda: 100.0)
    book.observe(loaded)
    book.observe([R.Loaded("qwen3:4b", 2.0, 1.0)])                             # a smaller later reading never lowers the largest seen
    assert book.observed_gb("qwen3:4b") == 3.0 and book.observed_gb("ghost") is None
    now = 10_000.0
    used = {"qwen3:4b": now - 100, "emb": now - 5000}
    assert R.idle_candidates(loaded, used, set(), now) == ["emb"]
    assert R.idle_candidates(loaded, used, {"emb"}, now) == [] and R.idle_candidates(loaded, used, set(), now, keep={"emb"}) == []
    assert R.idle_candidates(loaded, {}, set(), now)[0] in ("qwen3:4b", "emb")           # never used = idle the longest


# ---- scheduler -------------------------------------------------------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def test_priority_order_and_concurrency_limit():
    async def go():
        s = R.WorkloadScheduler(max_concurrent=1)
        order, gate = [], asyncio.Event()
        async def first(): await gate.wait(); order.append("first")
        def job(n): 
            async def w(): order.append(n)
            return w
        s.submit(R.P2, "first", first)
        await asyncio.sleep(0)
        low, high, mid = s.submit(R.P4, "low", job("low")), s.submit(R.P1, "voice", job("voice")), s.submit(R.P3, "index", job("index"))
        assert s.stats()["queued"][0][1] == "voice" and len(s.stats()["running"]) == 1
        gate.set()
        await asyncio.gather(low.future, high.future, mid.future)
        return order
    assert run(go()) == ["first", "voice", "index", "low"]


def test_p0_runs_even_when_busy_and_vram_is_reserved():
    async def go():
        s = R.WorkloadScheduler(max_concurrent=1, vram_budget=lambda: 4.0, margin_gb=0.5)
        gate = asyncio.Event()
        async def big(): await gate.wait(); return "big"
        async def small(): return "small"
        a = s.submit(R.P2, "model a", big, vram_gb=3.0)
        await asyncio.sleep(0)
        b = s.submit(R.P2, "model b", small, vram_gb=3.0)                       # would exceed 4 GB with the reservation: waits
        emergency = s.submit(R.P0, "stop everything", small)                    # never held back
        assert await emergency.future == "small" and b.state == "queued" and s.reserved_gb == 3.0
        gate.set()
        return await a.future, await b.future
    assert run(go()) == ("big", "small")


def test_hold_parks_background_work_until_released_and_unknown_vram_is_not_invented():
    async def go():
        s = R.WorkloadScheduler(max_concurrent=3)                                # budget unknown -> no invented limit
        s.hold(R.P3)
        async def w(): return 1
        bg, user = s.submit(R.P3, "indexing", w), s.submit(R.P2, "user task", w, vram_gb=2.0)
        assert await user.future == 1 and bg.state == "queued"
        s.hold(None)
        return await bg.future
    assert run(go()) == 1


def test_timeout_cancel_failure_and_cleanup():
    async def go():
        s = R.WorkloadScheduler(max_concurrent=1)
        async def slow(): await asyncio.sleep(5)
        async def boom(): raise ValueError("bad")
        async def ok(): return "ok"
        t = s.submit(R.P2, "slow", slow, timeout=0.05)
        with pytest.raises(TimeoutError):
            await t.future
        f = s.submit(R.P2, "boom", boom)
        with pytest.raises(ValueError):
            await f.future
        gate = asyncio.Event()
        async def wait(): await gate.wait()
        r = s.submit(R.P2, "running", wait)
        await asyncio.sleep(0.01)
        q = s.submit(R.P3, "queued", ok)
        assert s.cancel(q) and q.state == "cancelled" and s.cancel(r)
        await asyncio.sleep(0.05)
        assert r.state == "cancelled" and s.stats()["running"] == [] and [j.state for j in s.history][:2] == ["timeout", "failed"]
        assert await s.run(R.P1, "after", ok) == "ok"
        with pytest.raises(ValueError):
            s.submit(9, "bad priority", ok)
    run(go())


# ---- plugin job object -----------------------------------------------------------------------------------------------------------------

def test_job_limit_is_a_noop_off_windows_and_probe_still_works(tmp_path):
    import sys
    from app.core import plugin_sandbox as PS
    if sys.platform != "win32":
        assert PS.limit_windows(object()) is None
    PS.close_job(None)
    (tmp_path / "p.py").write_text('"""x"""\nfrom app.security.permissions import RiskLevel\nfrom app.tools.base import NoArgs, Tool, ToolResult\n'
                                   'TOOLS = [Tool("probe_job", "d", lambda a, c: ToolResult.success("x"), NoArgs, RiskLevel.SAFE)]\n')
    assert PS.probe(tmp_path / "p.py")["tools"] == {"probe_job": 0}
    (tmp_path / "slow.py").write_text("import time\ntime.sleep(30)\n")
    with pytest.raises(PS.ProbeError, match="longer"):
        PS.probe(tmp_path / "slow.py", timeout=2)
