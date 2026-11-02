"""Unified request context + agent blackboard (pure logic; the live readers are exercised with fakes)."""

from __future__ import annotations

import pytest

from app.core import blackboard as B
from app.core import request_context as R

NOW = 1_000_000.0


def item(section, text, evidence=R.VERIFIED, conf=1.0, age=None, life=None, **kw):
    return R.Item(section, text, evidence, kw.pop("source", "test"), conf, None if age is None else NOW - age, life, **kw)


def test_only_relevant_items_are_chosen_and_always_items_stay():
    items = [item("permissions", "no emergency switch is on"), item("project", "project Tourism (python)"),
             item("project", "project Billing (java)"), item("memory", "likes dark mode")]
    ctx = R.build("start the tourism server", items, now=NOW)
    texts = [i.text for i in ctx.items]
    assert "no emergency switch is on" in texts and "project Tourism (python)" in texts
    assert not any("Billing" in t or "dark mode" in t for t in texts) and ctx.dropped == 2


def test_evidence_labels_and_unknowns_are_shown_not_hidden():
    items = [item("memory", "uses Chrome", R.RETRIEVED), item("memory", "maybe studies at night chrome", R.INFERRED, 0.5)]
    out = R.build("open chrome", items, unknown=["devices (no database)"], now=NOW).render()
    assert "[retrieved] uses Chrome" in out and "[inferred] maybe studies" in out and "not available: devices (no database)" in out
    assert "do not guess" in out


def test_freshness_stale_flagged_and_expired_dropped():
    fresh = item("resource", "CPU 10% cpu", age=10, life=60)
    stale = item("resource", "CPU 50% cpu old", age=90, life=60)
    gone = item("resource", "CPU 99% cpu ancient", age=600, life=60)
    ctx = R.build("cpu load", [fresh, stale, gone], now=NOW)
    assert [i.text for i in ctx.items] == ["CPU 10% cpu", "CPU 50% cpu old"]
    assert "may be out of date" in ctx.render() and ctx.dropped == 1


def test_budget_is_respected():
    items = [item("memory", f"note about budget number {i} " + "x" * 120) for i in range(30)]
    ctx = R.build("budget", items, now=NOW)
    assert sum(len(i.line(NOW)) for i in ctx.items) <= R.SECTIONS["memory"][0] and len(ctx.items) < 30 and ctx.dropped >= 25


def test_private_items_only_when_named():
    items = [item("memory", "friend Rafi birthday", R.RETRIEVED, private=True)]
    assert R.build("what is on today", items, now=NOW).items == []
    assert len(R.build("rafi birthday", items, now=NOW).items) == 1


def test_bangla_and_banglish_words_match():
    items = [item("project", "project পর্যটন ম্যানেজমেন্ট (python)"), item("project", "project tourism (python)")]
    assert [i.text for i in R.build("পর্যটন project ta open koro", items, now=NOW).items][0].startswith("project পর্যটন")


def test_clarify_asks_instead_of_guessing():
    empty = R.build("open that project", [item("permissions", "ok")], now=NOW)
    assert R.clarify("open that project", empty) == "Which project do you mean?"
    one = R.build("open that project", [item("project", "project Tourism (python)", conf=0.9)], now=NOW)
    assert R.clarify("open that project", one) is None            # one confident candidate is not ambiguous... but it must also be relevant
    assert R.clarify("continue where I left off", empty).startswith("Which task")
    assert R.clarify("what time is it", empty) is None


def test_clarify_two_equal_projects_asks_which_and_a_named_one_does_not():
    a, b = item("project", "project Tourism (python)", conf=0.8), item("project", "project Billing (java)", conf=0.8)
    ctx = R.build("open that project tourism billing", [a, b], now=NOW)
    ask = R.clarify("open that project", ctx)
    assert ask and "Which project" in ask and "Tourism" in ask and "Billing" in ask
    assert R.clarify("open the tourism project", ctx) is None


def test_stale_or_unsure_items_do_not_resolve_a_reference():
    weak = R.build("that project", [item("project", "project Tourism that project", conf=0.3)], now=NOW)
    assert R.clarify("that project", weak) == "Which project do you mean?"
    old = R.build("that project", [item("project", "project Tourism that project", age=500, life=100)], now=NOW)
    assert R.clarify("that project", old) == "Which project do you mean?"


def test_cache_reuses_for_the_same_state_only():
    R._CACHE.clear()
    calls = []

    def make():
        calls.append(1)
        return R.build("x", [], now=NOW)

    a = R.cached("check my devices", "v1", make, now=NOW)
    b = R.cached("devices check my", "v1", make, now=NOW + 5)
    c = R.cached("check my devices", "v2", make, now=NOW + 5)
    d = R.cached("check my devices", "v1", make, now=NOW + 60)
    assert a is b and a is not c and len(calls) == 3 and d is not a


def test_gather_survives_a_broken_source_and_reports_it_as_unknown():
    class Ctx:
        database = None
        settings = None

        def get_memory(self):
            raise RuntimeError("boom")

    items, unknown = R.gather(Ctx(), "anything", now=NOW)
    assert any("memory" in u for u in unknown) and any("missions" in u for u in unknown)
    assert all(i.evidence in R.EVIDENCE for i in items)


# ------------------------------------------------------------------ blackboard


def test_repeating_a_state_changing_call_is_refused_but_reads_may_repeat():
    board = B.Blackboard()
    t = board.begin("organizer", "move_file", {"a": 1}, mutating=True)
    assert t.go
    board.finish(t, True, "moved a")
    again = board.begin("coder", "move_file", {"a": 1}, mutating=True)
    assert again.verdict == B.DONE and "organizer" in again.reason
    assert board.begin("coder", "move_file", {"a": 2}, mutating=True).go
    r = board.begin("researcher", "read_file", {"p": 1}, mutating=False)
    board.finish(r, True, "read")
    assert board.begin("researcher", "read_file", {"p": 1}, mutating=False).go


def test_same_call_in_flight_is_busy_and_failures_stop_blind_retries():
    board = B.Blackboard()
    t = board.begin("a", "send", {"x": 1}, mutating=True)
    assert board.begin("b", "send", {"x": 1}, mutating=True).verdict == B.BUSY
    board.finish(t, False, "no network")
    t2 = board.begin("a", "send", {"x": 1}, mutating=True)
    board.finish(t2, False, "no network")
    third = board.begin("a", "send", {"x": 1}, mutating=True)
    assert third.verdict == B.FAILED_BEFORE
    assert board.begin("a", "send", {"x": 2}, mutating=True).go          # a changed call is allowed


def test_delegation_loops_and_depth_are_refused():
    board = B.Blackboard()
    assert board.begin("planner", "t", {}, mutating=False, chain=("planner",)).verdict == B.LOOP
    assert board.begin("coder", "t", {}, mutating=False, chain=("a", "b", "c", "d")).verdict == B.LOOP
    ok = board.begin("coder", "t", {}, mutating=False, chain=("planner",))
    assert ok.go and ok.chain == ("planner", "coder")


def test_notes_are_typed_and_visible_to_other_agents():
    board = B.Blackboard()
    board.claim("researcher", "port 8000 is free", "verified")
    board.need("coder", "the project path")
    with pytest.raises(ValueError):
        board.claim("coder", "it will work", "certain")
    v = board.view("coder")
    assert v["claims"] == ["[verified] port 8000 is free"] and v["needs"] == ["coder: the project path"]


def test_for_run_gives_one_board_per_run():
    assert B.for_run("r1") is B.for_run("r1") and B.for_run("r1") is not B.for_run("r2")
    assert B.for_run("") is not B.for_run("")


def test_orchestrator_does_not_repeat_a_side_effect_across_two_plans():
    import asyncio
    from types import SimpleNamespace

    from app.core import orchestrator as O
    from app.security.permissions import RiskLevel
    from app.tools.base import ToolResult

    ran = []

    class Registry:
        def get(self, name):
            return SimpleNamespace(name=name, level=RiskLevel.MODERATE if name == "create_item" else RiskLevel.SAFE, category="files")

    class Exec:
        registry = Registry()
        context = SimpleNamespace(agent_contract=None)

        async def execute(self, call, preapproved=False):
            ran.append(call.name)
            return SimpleNamespace(result=ToolResult.success("made it"))

    steps = lambda: [O.AgentStep("organizer", "create_item", {"name": "report"}, "make report")]  # noqa: E731
    B._BOARDS.clear()
    from app.core.runs import Run, running

    async def both():
        with running(Run("internal", "two delegations in one request")):      # one request = one shared blackboard
            return await O.run_plan(Exec(), "g", steps(), approved=True), await O.run_plan(Exec(), "g", steps(), approved=True)

    first, second = asyncio.run(both())
    assert first.ok and second.ok and ran == ["create_item"]
    assert "not repeated" in second.steps[0].result


def test_parallel_steps_use_the_blackboard_too():
    import asyncio
    from types import SimpleNamespace

    from app.core import orchestrator as O
    from app.core.runs import Run, running
    from app.security.permissions import RiskLevel
    from app.tools.base import ToolResult

    calls = []

    class Registry:
        def get(self, name):
            return SimpleNamespace(name=name, level=RiskLevel.SAFE, category="files")

    class Exec:
        registry = Registry()
        context = SimpleNamespace(agent_contract=None)

        async def execute(self, call, preapproved=False):
            calls.append(call.name)
            return SimpleNamespace(result=ToolResult.failure("boom", "no network"))

    async def go():
        with running(Run("internal", "parallel")):
            for _ in range(3):
                steps = [O.AgentStep("researcher", "read_file", {"p": 1}, "read")]
                await O._run_parallel(Exec(), "g", steps, 2, 30.0, False)
            return steps

    steps = asyncio.run(go())
    assert calls == ["read_file", "read_file"]                       # the third identical failing call was refused, not run
    assert steps[0].state == "failed" and "failed 2 times" in steps[0].result


def test_new_operating_modes_resolve_and_only_make_jarvis_quieter():
    from app.core import modes

    for name, key in (("deep work mode", "deep work"), ("sleep mode", "sleep"), ("travel", "travel"), ("study mode", "study"), ("bedtime", "sleep")):
        assert modes.resolve(name).name == key
    assert modes.MODES["sleep"].speak == frozenset({"urgent"}) and modes.MODES["study"].suggestions
    assert modes.resolve("research mode").name == "research"                       # the old study alias did not swallow research
