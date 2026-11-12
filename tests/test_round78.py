from datetime import date

from app.core import schedule_sim as S

TODAY = date(2026, 11, 10)  # Tuesday


def todos():
    return [{"id": 1, "text": "Report (3h)", "due": "2026-11-11"}, {"id": 2, "text": "Slides [2h]", "due": "2026-11-12"},
            {"id": 3, "text": "Tidy desk", "due": None}]


def test_estimates_from_text_and_default():
    t = {x.key: x for x in S.tasks_from(todos())}
    assert t["1"].minutes == 180 and not t["1"].guessed
    assert t["2"].minutes == 120
    assert t["3"].minutes == S.DEFAULT_MIN and t["3"].guessed


def test_baseline_fits_and_nothing_mutated():
    src = todos()
    c = S.simulate(src, TODAY, hours_per_day=4)
    late = [o for o in c.baseline.outcomes if o.late_days > 0 or o.unscheduled]
    assert late == [] and src == todos()
    assert c.scenario.outcomes and not c.changes


def test_extra_daily_block_makes_tasks_late():
    c = S.simulate(todos(), TODAY, hours_per_day=4, extra_daily_hours=3, extra_label="studying")
    assert any(o.late_days > 0 for o in c.scenario.outcomes)
    assert not any(o.late_days > 0 for o in c.baseline.outcomes)
    assert "studying" in S.describe(c) and "Nothing was changed" in S.describe(c)


def test_postpone_and_drop_and_add():
    c = S.simulate(todos(), TODAY, 4, postpone={"1": 3}, drop=["3"], add=[{"text": "Exam prep", "hours": 9, "due": "2026-11-11"}])
    txt = "; ".join(c.changes)
    assert "postpone" in txt and "drop" in txt and "Exam prep" in txt
    assert any(o.key == "new1" and o.late_days > 0 for o in c.scenario.outcomes)


def test_postpone_without_due_says_so_and_unscheduled():
    c = S.simulate(todos(), TODAY, 4, postpone={"3": 2})
    assert "no due date" in c.changes[0]
    big = S.simulate([{"id": 9, "text": "Huge (500h)", "due": None}], TODAY, 4, horizon=3)
    assert big.baseline.outcomes[0].unscheduled


def test_confidence_never_high_and_low_when_guessing():
    assert S.simulate(todos(), TODAY).confidence == "medium"
    assert S.simulate([{"id": i, "text": "x", "due": None} for i in range(4)], TODAY).confidence == "low"
    assert S.simulate([], TODAY).confidence == "low"


def test_busy_time_reduces_capacity():
    free = S.simulate(todos(), TODAY, 4)
    busy = S.simulate(todos(), TODAY, 4, busy={TODAY: 240, TODAY.replace(day=11): 240})
    assert free.baseline.capacity_min > busy.baseline.capacity_min
    assert any(o.late_days > 0 for o in busy.baseline.outcomes)


# ---- application adapters ------------------------------------------------------------------------------------------
import asyncio

from app.core import app_adapters as A


def test_adapter_detect_inspect_actions():
    ad = A.ApplicationAdapter.detect_application("EXCEL.EXE")
    assert ad.name == "Excel" and "excel_control" in ad.inspect_application().structured
    assert "save" in ad.get_available_actions()
    unk = A.ApplicationAdapter.detect_application("mystery")
    assert unk.inspect_application().uia == "unknown" and unk.get_available_actions() == []


def test_structured_tools_exist_in_registry():
    from app.tools.catalog import build_default_registry

    reg = build_default_registry()
    for tools in A.STRUCTURED_TOOLS.values():
        for t, _ in tools:
            assert reg.get(t) is not None, t
    assert reg.get("press_control") is not None


def test_route_order():
    code = A.ApplicationAdapter.detect_application("code")
    assert code.plan_action("save").route == A.SHORTCUT            # poor UIA, shortcut known
    assert code.plan_action("anything", tool="vscode").route == A.STRUCTURED
    assert A.ApplicationAdapter.detect_application("notepad").plan_action("save").route == A.CONTROL
    assert code.plan_action("teleport").route == A.NONE


def test_execute_verified_unverified_and_failure():
    ad = A.ApplicationAdapter.detect_application("notepad")
    calls = []

    async def ok(tool, args):
        calls.append((tool, args))

    async def boom(tool, args):
        raise RuntimeError("control not found")

    async def seen(plan):
        return True

    r = asyncio.run(ad.execute_action("save", ok, seen))
    assert r.ok and r.verified and calls == [("press_control", {"name": "save"})]
    r = asyncio.run(ad.execute_action("save", ok, None))
    assert r.ok and not r.verified and "could not be verified" in r.message      # no verifier: never claimed as success
    r = asyncio.run(ad.execute_action("save", boom, seen))
    assert not r.ok and "failed in Notepad" in r.message
    r = asyncio.run(A.ApplicationAdapter.detect_application("code").execute_action("save", ok))
    assert not r.ok and "Ctrl+S" in r.message and len(calls) == 2               # shortcut offered, nothing pressed


def test_risky_dialog_never_auto_answered():
    msg = A.ApplicationAdapter.detect_application("winword").recover_from_error("save", dialog_text="Do you want to save changes?")
    assert "will not answer" in msg


# ---- inference gateway: live calls are tracked, never queued -------------------------------------------------------
from app.core import inference_gateway as G


def test_observe_tracks_chat_lifecycle():
    gw = G.Gateway()

    async def go():
        async with gw.observe("chat", "m1") as req:
            assert req.state == G.RUNNING and gw.snapshot()["active"][0][1] == "chat"
        return req

    req = asyncio.run(go())
    assert req.state == G.COMPLETED and [e[0] for e in req.events] == [G.QUEUED, G.RUNNING, G.COMPLETED]
    assert gw.snapshot()["active"] == [] and gw.snapshot()["recent"][-1][2] == G.COMPLETED


def test_observe_failure_cancel_and_cancel_api():
    gw = G.Gateway()

    async def fail():
        async with gw.observe("chat", "m") as r:
            raise ValueError("model gone")

    try:
        asyncio.run(fail())
    except ValueError:
        pass
    assert gw.history[-1].state == G.FAILED and "model gone" in gw.history[-1].error

    async def cancelled():
        async with gw.observe("voice", "m"):
            raise asyncio.CancelledError()

    try:
        asyncio.run(cancelled())
    except BaseException:
        pass
    assert gw.history[-1].state == G.CANCELLED
    assert gw.cancel("inf-nope") is False


def test_manager_chat_goes_through_gateway(monkeypatch):
    import app.ai.manager as M

    gw = G.Gateway()
    monkeypatch.setattr(M, "_gateway", lambda: gw)

    class Fake(M.AIManager):
        def __init__(self):
            pass

        async def _prepare(self, *a, **k):
            return type("R", (), {"model": "fake"})()

        async def _chat_prepared(self, request):
            return "reply"

    assert asyncio.run(Fake().chat([])) == "reply"
    assert gw.history[-1].capability == "chat" and gw.history[-1].model == "fake" and gw.history[-1].state == G.COMPLETED


def test_whatif_busy_days_and_weekends():
    from datetime import date
    from app.core import schedule_sim as S

    todos = [{"id": 1, "text": "a (4h)", "due": "2026-11-16"}]
    free = S.simulate(todos, date(2026, 11, 13), 4.0, horizon=7)
    assert free.baseline.outcomes[0].finish == date(2026, 11, 13)
    busy = S.simulate(todos, date(2026, 11, 13), 4.0, busy={date(2026, 11, 13): 240}, horizon=7, skip_weekdays=(5, 6))
    assert busy.baseline.outcomes[0].finish == date(2026, 11, 16)
    assert "weekdays only" in busy.assumptions[0]
