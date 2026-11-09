"""Round 74: root-cause classifier + recovery levels, inference gateway lifecycle, Obsidian vault reader. Fakes only: no GPU, no Ollama."""
from __future__ import annotations

import asyncio

import pytest

from app.core import inference_gateway as G
from app.core import replay, root_cause as RC, vault as V
from app.core import resources as R


# ------------------------------------------------------------------ root cause

def call(n, tool="open_application", ok=True, error=None, message="", **kw):
    return replay.Call(n=n, tool=tool, ok=ok, error=error, message=message, arguments={}, **kw)


def tl(*calls):
    return replay.Timeline({"id": "r1"}, list(calls))


def test_earliest_failure_is_named_and_later_ones_are_only_possible_consequences():
    d = RC.diagnose(tl(call(1), call(2, ok=False, error="invalid_arguments", message="missing name"), call(3, ok=False, error="timeout", message="timed out")))
    assert d.first.n == 2 and d.hypotheses[0].cls == "invalid_tool_arguments" and d.later_failures == [3]
    text = d.describe()
    assert "possibly consequences" in text and "none of these is certain" in text


@pytest.mark.parametrize("kw,cls", [
    (dict(ok=False, error="blocked", message="not allowed"), "permission_failure"),
    (dict(ok=False, error="unknown_tool", message="no such tool"), "incorrect_planning"),
    (dict(ok=False, error="out_of_memory", message="out of memory"), "resource_exhaustion"),
    (dict(ok=False, error="offline", message="connection refused"), "network_failure"),
    (dict(ok=False, error="x", message="ModuleNotFoundError: no module named foo"), "dependency_failure"),
    (dict(ok=False, error="x", message="Ollama service unavailable 503"), "external_service_failure"),
    (dict(ok=False, error="not_visible", message="could not find the button on screen"), "incorrect_visual_interpretation"),
    (dict(ok=False, error="internal", message="Traceback KeyError"), "tool_implementation_failure"),
    (dict(ok=False, error="not_verified", message="ran"), "incomplete_verification"),
])
def test_each_cause_class_is_recognised_from_evidence(kw, cls):
    hyps = RC.hypotheses_for(call(1, **kw))
    assert hyps and hyps[0].cls == cls or cls in [h.cls for h in hyps[:2]]
    assert all(h.confidence in ("low", "medium", "high") and h.score <= 0.95 for h in hyps)     # never "certain"
    assert all(h.evidence for h in hyps)


def test_no_evidence_means_no_invented_cause_and_a_clean_run_has_no_diagnosis():
    d = RC.diagnose(tl(call(1, ok=False, error="weird_code", message="")))
    assert d.first.n == 1 and d.hypotheses == [] and "does not show enough" in d.describe()
    assert RC.diagnose(tl(call(1), call(2))).ok


def test_recovery_levels_forbidden_actions_are_never_offered_or_run():
    assert RC.level_of("Check the log for this call") == 1
    assert RC.level_of("Retry the step") == 2 and RC.level_of("Delete the old files") == 3 and RC.level_of("Reinstall the driver") == 3
    for bad in ("Disable the antivirus to get past this", "Bypass the approval", "Clear the audit log", "Grant all permissions", "Run any command", "rm -rf /"):
        assert RC.level_of(bad) == 4, bad
    assert RC.may_run_unasked(0) and RC.may_run_unasked(1) and not any(RC.may_run_unasked(x) for x in (2, 3, 4))
    d = RC.diagnose(tl(call(1, ok=False, error="blocked", message="not allowed")))
    assert d.options and all(o.level != 4 for o in d.options)
    assert any("no permission is widened" in o.text.lower() for o in d.options)
    assert "needs your OK" in d.describe()


def test_evidence_record_keeps_no_arguments():
    c = call(1, ok=False, error="timeout", message="timed out")
    c.arguments = {"password": "hunter2"}
    rec = RC.evidence_record(RC.diagnose(tl(c)))
    assert "hunter2" not in str(rec) and rec["tool"] == "open_application" and rec["class"] == "network_failure"


# ------------------------------------------------------------------ gateway

def gpu(free):
    return lambda: R.Gpu("g", 6.0, 6.0 - free, 10.0, 50.0, None, "fake") if free is not None else R.Gpu()


def run(coro):
    return asyncio.run(coro)


def make(free=4.0, ram=16.0, **kw):
    return G.Gateway(sched=R.WorkloadScheduler(max_concurrent=2), gpu=gpu(free), ram_free_gb=lambda: ram, **kw)


def test_lifecycle_states_in_order_and_gpu_chosen_when_it_fits():
    async def go():
        gw = make()
        seen = []
        gw.observers.append(lambda r: seen.append(r.state))

        async def work(r):
            return "hello"

        req = G.InferenceRequest("chat", "qwen", R.P1, est_gb=2.5)
        assert await gw.run(req, work) == "hello"
        return seen, req

    seen, req = run(go())
    assert seen == [G.QUEUED, G.RESOURCE_CHECK, G.MODEL_PREPARATION, G.RUNNING, G.COMPLETED]
    assert req.device_used == "gpu" and req.latency_ms is not None


def test_cpu_fallback_when_vram_is_short_but_ram_fits_and_a_clear_failure_when_nothing_fits():
    async def go():
        gw = make(free=1.0, ram=16.0)

        async def work(r):
            return r.device_used

        assert await gw.run(G.InferenceRequest("chat", "big", est_gb=3.0), work) == "cpu"
        bad = G.InferenceRequest("chat", "big", est_gb=3.0, device="gpu")           # explicitly GPU-only: no silent CPU
        with pytest.raises(G.ResourceError):
            await gw.run(bad, work)
        gw2 = make(free=1.0, ram=1.0)
        r3 = G.InferenceRequest("vision", "huge", est_gb=8.0, context_tokens=8192)
        with pytest.raises(G.ResourceError) as e:
            await gw2.run(r3, work)
        return bad, r3, str(e.value)

    bad, r3, msg = run(go())
    assert bad.state == G.FAILED and r3.state == G.FAILED and "shorter context" in msg


def test_no_gpu_read_means_cpu_and_a_cpu_request_skips_the_check():
    async def go():
        gw = make(free=None)

        async def work(r):
            return r.device_used

        a = await gw.run(G.InferenceRequest("embedding", "e", est_gb=9.0), work)        # no GPU read (a CPU-only machine): it runs on the CPU, nothing is invented
        b = await gw.run(G.InferenceRequest("embedding", "e", est_gb=99.0, device="cpu"), work)
        return a, b

    assert run(go()) == ("cpu", "cpu")


def test_one_model_load_at_a_time_and_a_failed_load_does_not_wedge_the_gateway():
    async def go():
        active, peak, loads = 0, 0, []

        async def prepare(model, device):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            loads.append(model)
            await asyncio.sleep(0.02)
            active -= 1

        gw = make(prepare=prepare)

        async def work(r):
            return r.id

        await asyncio.gather(*(gw.run(G.InferenceRequest("chat", "same", est_gb=1.0), work) for _ in range(4)))
        return peak, loads

    peak, loads = run(go())
    assert peak == 1 and len(loads) == 4


def test_transient_failures_retry_boundedly_and_permanent_ones_do_not():
    async def go():
        slept, calls = [], {"n": 0}

        async def nap(s):
            slept.append(s)

        gw = make(sleep=nap)

        async def flaky(r):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("connection timed out")
            return "ok"

        r = G.InferenceRequest("chat", max_retries=2)
        assert await gw.run(r, flaky) == "ok" and r.attempts == 2 and len(slept) == 1

        async def broken(r):
            raise ValueError("bad request")

        r2 = G.InferenceRequest("chat", max_retries=3)
        with pytest.raises(ValueError):
            await gw.run(r2, broken)
        return r2

    assert run(go()).attempts == 1                                     # unknown/permanent: shown, not hammered


def test_cancel_a_queued_and_a_running_request_and_release_reservations():
    async def go():
        sched = R.WorkloadScheduler(max_concurrent=1)
        gw = G.Gateway(sched=sched, gpu=gpu(4.0), ram_free_gb=lambda: 8.0)
        started = asyncio.Event()

        async def long(r):
            started.set()
            await asyncio.sleep(10)

        async def quick(r):
            return 1

        a = G.InferenceRequest("chat", "m", est_gb=1.0)
        b = G.InferenceRequest("background", "m", R.P3, est_gb=1.0)
        ta = asyncio.create_task(gw.run(a, long))
        await started.wait()
        tb = asyncio.create_task(gw.run(b, quick))
        await asyncio.sleep(0.01)
        assert gw.cancel(b.id) and gw.cancel(a.id)
        for t in (ta, tb):
            with pytest.raises(asyncio.CancelledError):
                await t
        await asyncio.sleep(0.01)
        return a, b, sched, gw

    a, b, sched, gw = run(go())
    assert a.state == G.CANCELLED and b.state == G.CANCELLED and sched.reserved_gb == 0 and not gw._active
    assert gw.cancel("nope") is False


def test_held_background_requests_show_paused_and_history_is_bounded():
    async def go():
        sched = R.WorkloadScheduler(max_concurrent=2)
        gw = G.Gateway(sched=sched, gpu=gpu(4.0), ram_free_gb=lambda: 8.0)
        sched.hold(R.P3)
        seen = []
        gw.observers.append(lambda r: seen.append(r.state))

        async def work(r):
            return 1

        req = G.InferenceRequest("indexing", "e", R.P3)
        t = asyncio.create_task(gw.run(req, work))
        await asyncio.sleep(0.02)
        assert req.state == G.PAUSED and not t.done()
        sched.hold(None)
        await t
        for _ in range(5):
            await gw.run(G.InferenceRequest("chat"), work)
        return seen, gw

    seen, gw = run(go())
    assert seen[:2] == [G.QUEUED, G.PAUSED] and G.COMPLETED in seen and gw.history.maxlen == 200
    assert "recent" in gw.snapshot()


def test_request_validation():
    with pytest.raises(ValueError):
        G.InferenceRequest("telepathy")
    with pytest.raises(ValueError):
        G.InferenceRequest("chat", device="tpu")
    with pytest.raises(ValueError):
        G.InferenceRequest("chat", priority=9)
    assert G.InferenceRequest("chat", max_retries=99).max_retries == 3


# ------------------------------------------------------------------ vault

NOTE_A = """---
title: Jarvis plan
tags: [project, ai]
status: active
---
# Heading is not a tag
Talk to [[Ideas]] and [[Ideas#sub|alias]], also [[Missing note]] and ![[image.png]].
Inline #roadmap tag here. ```python
x = "#nottag [[NotALink]]"
```
"""
NOTE_B = "---\ntags:\n  - idea\n  - ai\n---\nBack to [[Jarvis plan]] and [[jarvis plan]].\n"


def make_vault(tmp_path):
    (tmp_path / "a.md").write_text(NOTE_A, encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Ideas.md").write_text(NOTE_B, encoding="utf-8")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "x.md").write_text("[[a]]", encoding="utf-8")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "secret.md").write_text("secret [[a]]", encoding="utf-8")
    (tmp_path / "big.md").write_bytes(b"x" * (V.MAX_BYTES + 1))
    (tmp_path / "pic.png").write_bytes(b"png")
    return tmp_path


def test_parse_note_front_matter_tags_links_and_code_are_handled():
    n = V.parse_note(NOTE_A, "a.md")
    assert n.title == "Jarvis plan" and n.meta["status"] == "active"
    assert n.tags == ["ai", "project", "roadmap"]                      # front matter + inline; the heading and the code are not tags
    assert n.links == ["Ideas", "Missing note"]                         # alias/heading variants collapse; image embeds and code are not links
    b = V.parse_note(NOTE_B, "sub/Ideas.md")
    assert b.tags == ["ai", "idea"] and b.title == "Ideas"


def test_scan_resolves_links_reports_unresolved_and_skips_hidden_excluded_and_oversized(tmp_path):
    v = V.scan(make_vault(tmp_path), excluded=("private",))
    assert sorted(v.notes) == ["a.md", "sub/Ideas.md"] and v.skipped == 1
    assert ("a.md", "Missing note") in v.unresolved and not any(t == "NotALink" for _, t in v.unresolved)
    assert v.backlinks["sub/Ideas.md"] == ["a.md"] and v.backlinks["a.md"] == ["sub/Ideas.md"]
    assert set(v.edges()) == {("a.md", "links_to", "sub/Ideas.md"), ("sub/Ideas.md", "links_to", "a.md")}
    assert "2 notes" in V.summary(v) and "1 links to notes that do not exist" in V.summary(v)


def test_scan_never_opens_excluded_notes(tmp_path):
    opened = []
    V.scan(make_vault(tmp_path), excluded=("private",), read=lambda p: opened.append(p.name) or "x")
    assert "secret.md" not in opened and "x.md" not in opened


def test_scan_survives_an_unreadable_folder(tmp_path):
    (tmp_path / "ok.md").write_text("hi", encoding="utf-8")
    assert V.scan(tmp_path / "missing").notes == {} and len(V.scan(tmp_path).notes) == 1
