import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core import leases as L, opportunities as O, predictions as P, tech_experience as T
from app.memory.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.initialize()
    return d


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---- leases ----------------------------------------------------------------------------------------------------------
def test_no_lease_means_unchanged(db):
    assert L.LeaseStore(db).authorize("m1", "delete_file", 2, False, {}) == (True, "")


def test_lease_restricts_tool_level_network_folder(db, tmp_path):
    ck = Clock()
    s = L.LeaseStore(db, ck)
    s.grant("m1", ["read_file", "browser_open"], hours=1, folder=str(tmp_path), network=False, max_level=1, max_calls=2)
    assert s.authorize("m1", "delete_file", 0, False, {})[0] is False
    assert "risk level" in s.authorize("m1", "read_file", 2, False, {})[1]
    assert "network" in s.authorize("m1", "browser_open", 0, True, {})[1]
    assert "outside" in s.authorize("m1", "read_file", 0, False, {"path": str(tmp_path.parent / "x.txt")})[1]
    assert "outside" in s.authorize("m1", "read_file", 0, False, {"path": "../x.txt"})[1]
    assert s.authorize("m1", "read_file", 0, False, {"path": str(tmp_path / "a.txt")}) == (True, "")
    assert s.get("m1").calls == 1                       # denials do not spend the budget


def test_lease_budget_expiry_revoke_block_not_fallback(db):
    ck = Clock()
    s = L.LeaseStore(db, ck)
    s.grant("m", ["t"], hours=1, max_calls=1)
    assert s.authorize("m", "t", 0, False, {})[0]
    assert "used up" in s.authorize("m", "t", 0, False, {})[1]
    s.grant("m", ["t"], hours=1)
    ck.t += 3601
    assert "expired" in s.authorize("m", "t", 0, False, {})[1]
    s.grant("m", ["t"], hours=1)
    assert s.revoke("m") and "revoked" in s.authorize("m", "t", 0, False, {})[1]
    assert s.grant("m", ["t"], hours=999).expires <= ck.t + L.MAX_HOURS * 3600


def test_executor_enforces_lease_for_missions(db):
    from app.agent.tool_manager import ToolExecutor

    class Ctx:
        database = db

    class FakeTool:
        name, needs_network = "write_file", False

    class Call:
        name, arguments = "write_file", {"path": "x"}

    ex = ToolExecutor.__new__(ToolExecutor)
    ex.context = Ctx()
    audits = []
    ex._audit = lambda event, call, **kw: audits.append((event, kw))
    assert ex._lease_denies(FakeTool(), Call(), "", 1) is None            # not a mission
    assert ex._lease_denies(FakeTool(), Call(), "m9", 1) is None          # mission without a lease
    L.LeaseStore(db).grant("m9", ["read_file"])
    r = ex._lease_denies(FakeTool(), Call(), "m9", 1)
    assert r is not None and not r.ok and r.error == "lease_denied" and audits[0][0] == "lease_denied"


# ---- predictions -----------------------------------------------------------------------------------------------------
def test_prediction_lifecycle_and_calibration(db):
    led = P.Ledger(db)
    assert not P.calibration(led.items())["enough"]
    ids = [led.predict("restart fixes service", f"c{i}", 0.9).id for i in range(6)]
    for i, pid in enumerate(ids):
        assert led.resolve(pid, i < 2, "checked the service status")
    assert led.resolve(ids[0], True, "again") is False                    # cannot be re-resolved
    cal = P.calibration(led.items())
    assert cal["enough"] and cal["hit_rate"] == 0.33 and cal["verdict"] == "overconfident"
    new, why = P.adjusted(0.9, led.items(topic="restart fixes service"))
    assert new < 0.9 and "33%" in why
    assert P.adjusted(0.9, [])[0] == 0.9
    with pytest.raises(ValueError):
        led.resolve(led.predict("t", "c", 0.5).id, True, "  ")            # no evidence, no outcome
    with pytest.raises(ValueError):
        led.predict("t", "c", 1.5)


def test_adjust_never_raises_confidence(db):
    led = P.Ledger(db)
    for _ in range(6):
        led.resolve(led.predict("x", "c", 0.5).id, True, "seen")
    assert P.adjusted(0.5, led.items(topic="x"))[0] == 0.5


# ---- technical experience -------------------------------------------------------------------------------------------
def test_experience_recall_applicability(db):
    st = T.Store(db)
    st.add("MySQL will not start after update", "restored my.ini and restarted the service", "ERROR 2002 can't connect to local MySQL server",
           {"os": "windows 11", "mysql": "8.0"}, ["reinstalling the connector", " "], "service running and a test query returned rows")
    st.add("MySQL connection refused", "opened port 3306", "ERROR 2002 can't connect", {"mysql": "8.0"})
    same = st.recall("mysql does not start", "ERROR 2002 can't connect to local MySQL server", {"mysql": "8.0"})
    assert same[0].verdict == "applies" and same[0].exp.failed == ["reinstalling the connector"]
    assert any(m.verdict == "unverified fix" for m in same) and same[0].score > same[-1].score
    other = st.recall("mysql does not start", "ERROR 2002 can't connect to local MySQL server", {"mysql": "9.1"})
    assert other[0].verdict == "may not apply" and "then 8.0, now 9.1" in other[0].differs[0]
    assert st.recall("printer is out of paper") == [] and "nothing to reuse" in T.describe([])
    assert "did NOT work" in T.describe(same) and "nothing was run" in T.describe(same)
    assert st.delete(same[0].exp.id) and len(st.all()) == 1


def test_experience_old_flag():
    e = T.Experience("a", "disk full error", "", {}, [], "cleaned temp", "free space checked", 0.0)
    m = T.recall([e], "disk full error", "", {}, now=T.STALE_DAYS * 86400 + 10)
    assert m[0].old


# ---- opportunities ---------------------------------------------------------------------------------------------------
def test_opportunities_rank_dismiss_and_evidence():
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    ms = O.from_missions([{"id": "m1", "title": "Backup", "state": "paused", "note": "", "updated_at": (now - timedelta(days=5)).isoformat()},
                          {"id": "m2", "title": "Fresh", "state": "paused", "note": "", "updated_at": now.isoformat()}], now)
    assert [o.key for o in ms] == ["opp:mission:m1"] and "5 days" in ms[0].evidence
    gs = O.from_goals([{"id": 1, "title": "Ship app", "tasks": []}, {"id": 2, "title": "Has plan", "tasks": [{"id": 1}]}])
    pr = O.from_commitments([{"kind": "i_owe", "text": "send report", "due": "2026-11-01"}, {"kind": "they_owe", "text": "x", "due": "2026-11-01"}], "2026-11-11")
    assert len(gs) == 1 and len(pr) == 1
    assert O.from_calibration({"enough": False}) == []
    shown, hidden = O.rank(ms + gs + pr, blocked=lambda k: "muted" if k == "opp:goal:1" else "")
    assert hidden == 1 and shown[0].score >= shown[-1].score
    text = O.describe(shown, hidden, ["failing tools"])
    assert "evidence:" in text and "Could not check: failing tools" in text
    assert "No opportunity with real evidence" in O.describe([])


def test_collect_on_empty_db_is_quiet(db):
    items, failed = O.collect(db)
    assert items == [] or all(o.evidence for o in items)


# ---- tools -----------------------------------------------------------------------------------------------------------
def test_tools_registered_and_levels():
    from app.security.permissions import RiskLevel
    from app.tools import governance_tools as G
    from app.tools.catalog import build_default_registry

    reg = build_default_registry()
    for n in ("lease", "predictions", "tech_experience", "opportunities"):
        assert reg.get(n) is not None
    t = reg.get("lease")
    assert t.level_for(G.LeaseArgs(action="list"), None) == RiskLevel.SAFE
    assert t.level_for(G.LeaseArgs(action="grant", mission="m", tools="x"), None) == RiskLevel.MODERATE


def test_lease_tool_rejects_unknown_tools(db):
    from app.tools import governance_tools as G
    from app.tools.base import ToolError
    from app.tools.catalog import build_default_registry

    class Ctx:
        database = db

        def get_tool_registry(self):
            return build_default_registry()

    with pytest.raises(ToolError):
        G.lease(G.LeaseArgs(action="grant", mission="m", tools="read_file, make_coffee"), Ctx())
    out = G.lease(G.LeaseArgs(action="grant", mission="m", tools="list_todos"), Ctx())
    assert out.ok and "only restricts" in out.message
    assert "revoked" in G.lease(G.LeaseArgs(action="revoke", mission="m"), Ctx()).message


# ==== round 80: the remaining items =====================================================================================
def test_auto_lease_from_plan_and_renewal(db):
    ck = Clock()
    s = L.LeaseStore(db, ck)
    plan = [("read_file", 2, False), ("browser_open", 2, True), ("read_file", 2, False)]
    a = L.ensure_auto(s, "m1", plan)
    assert a.auto and a.tools == ("browser_open", "read_file") and a.network and a.max_calls == 20
    assert s.authorize("m1", "delete_file", 0, False, {})[0] is False
    ck.t += L.AUTO_HOURS * 3600 + 1                                     # paused over a day: the automatic lease is renewed from the same plan
    assert s.get("m1").state(ck()) == "expired"
    assert L.ensure_auto(s, "m1", plan).state(ck()) == "active"
    s.revoke("m1")
    assert L.ensure_auto(s, "m1", plan).revoked                         # a revoked lease is never re-granted automatically
    mine = s.grant("m2", ["read_file"], hours=0.05)
    ck.t += 3600
    assert L.ensure_auto(s, "m2", plan).auto is False and mine.tools == ("read_file",)   # your own lease is never replaced, even expired
    assert L.ensure_auto(s, "m3", []) is None


def test_mission_run_grants_lease_and_records_predictions(db):
    from app.core import missions as M

    class Res:
        def __init__(self, ok):
            self.ok, self.message, self.error, self.data = ok, "done" if ok else "boom", None if ok else "failed", {}

    class Exe:
        def __init__(self, ok):
            self.result, self.level = Res(ok), 0

    class Tool:
        needs_network, level, verify = False, 0, None

    class Reg:
        def get(self, name):
            return Tool()

    class Executor:
        registry = Reg()
        context = type("C", (), {"settings": type("S", (), {"mission_auto_lease": True})()})()
        calls = []

        async def execute(self, call, **kw):
            self.calls.append(call.name)
            return Exe(call.name != "bad_tool")

    store = M.MissionStore(db)
    mission = store.create("Test", [M.Step(1, "first", "list_todos", {}), M.Step(2, "second", "bad_tool", {})])
    asyncio.run(M.run(mission, store, Executor(), approved=True))
    lease = L.LeaseStore(db).get(mission.id)
    assert lease is not None and lease.auto and set(lease.tools) == {"list_todos", "bad_tool"}
    items = P.Ledger(db).items()
    by = {p.topic: p for p in items}
    assert by["tool:list_todos"].outcome == "right" and by["tool:bad_tool"].outcome == "wrong"


def test_auto_lease_can_be_switched_off(db):
    from app.core import missions as M

    class Executor:
        registry = None
        context = type("C", (), {"settings": type("S", (), {"mission_auto_lease": False})()})()

    m = type("Mi", (), {"id": "mx", "steps": []})()
    M._auto_lease(m, M.MissionStore(db), Executor())
    assert L.LeaseStore(db).get("mx") is None


def test_prediction_prune(db):
    led = P.Ledger(db, Clock())
    for i in range(5):
        led.clock.t += 1
        led.predict("t", f"c{i}", 0.5)
    assert P.prune(db, keep=2) == 3 and len(led.items()) == 2


def test_mine_experience_candidates_hides_secrets():
    rows = [{"ts": 0, "tool": "open_file", "ok": False, "error": "not_found", "message": "No such file", "arguments": {"path": "a.txt", "api_key": "S3CRET"}},
            {"ts": 60, "tool": "open_file", "ok": True, "error": "", "message": "", "arguments": {"path": "b.txt"}},
            {"ts": 100, "tool": "x", "ok": False, "error": "e", "message": "m", "arguments": {}},
            {"ts": 5000, "tool": "x", "ok": True, "error": "", "message": "", "arguments": {"a": 1}}]      # too late: not a candidate
    c = T.mine(rows)
    assert len(c) == 1 and "open_file failed" in c[0]["problem"] and "b.txt" in c[0]["solution"]
    assert "S3CRET" not in str(c) and "<hidden>" in c[0]["failed"][0]


def test_idle_gate_and_worker():
    from app.core import idle_work as I

    assert I.should_run(30, 10, False, 5)[0] is False
    assert I.should_run(700, 10, True, 5) == (False, "on battery")
    assert "busy" in I.should_run(700, 10, False, 90)[1]
    assert I.should_run(700, 10, False, 5, background_allowed=False)[0] is False
    assert I.should_run(700, 10, None, None) == (True, "idle")

    class Store(dict):
        def set(self, k, v):
            self[k] = v

    ck = Clock()
    ran = []
    jobs = (I.Job("a", 1, lambda db: ran.append("a") or "did a"), I.Job("b", 1, lambda db: 1 / 0), I.Job("c", 1, lambda db: ran.append("c") or "did c"))
    st = Store()
    idle = {"v": True}
    w = I.IdleWorker(None, st, jobs, ck, still_idle=lambda: idle["v"])
    done = w.run_due()
    assert [d["job"] for d in done] == ["a", "b", "c"] and done[1]["ok"] is False and "failed" in done[1]["result"]
    assert w.run_due() == []                                              # nothing due again (also not the failed one: no hot retry)
    ck.t += 3601
    idle["v"] = False
    assert w.run_due() == [] and ran == ["a", "c"]                        # you came back: nothing runs
    text = I.describe(st[I.LOG_KEY], ck())
    assert "FAILED" in text and "did a" in text
    assert "No idle work has run yet" in I.describe([], ck()) and "switched off" in I.describe([], ck(), enabled=False)


def test_idle_budget_and_briefing_line():
    from app.core import idle_work as I

    class Store(dict):
        def set(self, k, v):
            self[k] = v

    t = {"v": 0.0}

    def slow(db):
        t["v"] += 30
        return "slow"

    w = I.IdleWorker(None, Store(), (I.Job("s1", 1, slow), I.Job("s2", 1, slow)), Clock(), budget_s=20, timer=lambda: t["v"])
    assert [d["job"] for d in w.run_due()] == ["s1"]                      # the budget stops the second job
    st = Store()
    assert I.briefing_line(st, 100.0) == ""
    st.set(I.OPP_KEY, {"at": 100.0, "items": [{"title": "Mission 'X' is paused", "evidence": "paused for 5 days", "key": "k"}]})
    assert "Mission 'X' is paused" in I.briefing_line(st, 200.0)
    assert I.briefing_line(st, 100.0 + 19 * 3600) == ""                   # old findings are not presented as fresh


def test_idle_real_jobs_run_on_empty_db(db):
    from app.core import idle_work as I
    from app.memory.settings_store import SettingsStore

    done = I.IdleWorker(db, SettingsStore(db)).run_due()
    assert len(done) == len(I.JOBS) and all(d["ok"] for d in done), done


def test_otel_export_redacts_by_default(db, tmp_path):
    import json

    from app.core import otel_export as OT

    run = {"id": "r1", "source": "typed", "text": "open my secret file", "state": "completed", "started_at": "2026-11-11T10:00:00+00:00", "ended_at": "2026-11-11T10:00:02+00:00", "elapsed_ms": 2000}
    rows = [{"ts": "2026-11-11T10:00:01+00:00", "event": "tool_result", "tool": "read_file", "permission_level": 0, "decision": "allow",
             "details": json.dumps({"ok": True, "elapsed_ms": 500, "message": "PRIVATE CONTENT", "arguments": {"path": "c:/x.txt", "token": "abc"}})},
            {"ts": "2026-11-11T10:00:01+00:00", "event": "tool_call", "tool": "read_file", "permission_level": 0, "decision": "allow", "details": "{}"},
            {"ts": "2026-11-11T10:00:02+00:00", "event": "tool_result", "tool": "delete_file", "permission_level": 2, "decision": "blocked", "details": json.dumps({"ok": False, "error": "blocked"})}]
    doc = OT.build(run, rows)
    spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    text = json.dumps(doc)
    assert len(spans) == 3 and spans[1]["parentSpanId"] == spans[0]["spanId"] and len(spans[0]["traceId"]) == 32
    assert "PRIVATE CONTENT" not in text and "c:/x.txt" not in text and "secret file" not in text
    assert spans[2]["status"]["code"] == 2 and "execute_tool" in text
    assert int(spans[1]["endTimeUnixNano"]) - int(spans[1]["startTimeUnixNano"]) == 500_000_000
    full = json.dumps(OT.build(run, rows, include_arguments=True))
    assert "c:/x.txt" in full and "abc" not in full and "<redacted>" in full and "PRIVATE CONTENT" not in full
    with pytest.raises(LookupError):
        OT.export(db, "nope", tmp_path)


def test_integration_discovery_ranks_and_is_honest():
    from app.core import integration_discovery as D

    opts = D.discover("excel", which=lambda n: None, has_progid=lambda p: p == "Excel.Application", has_scheme=lambda n: False)
    routes = [o.route for o in opts]
    assert routes[0] == "existing tool" and "COM automation" in routes and routes[-1] == "UI Automation"
    git = D.discover("git", which=lambda n: "C:/git.exe" if n == "git" else None, has_progid=lambda p: False, has_scheme=lambda n: False)
    assert [o.route for o in git] == ["command line", "UI Automation"]
    sp = D.discover("spotify", which=lambda n: None, has_progid=lambda p: False, has_scheme=lambda n: True)
    assert [o.route for o in sp] == ["URL scheme", "UI Automation"]
    text = D.describe("spotify", sp)
    assert "Not checked" in text and "Nothing was installed" in text
    assert D.discover("  ") == [] and D.discover("a;b|c$(x)", which=lambda n: None, has_progid=lambda p: False, has_scheme=lambda n: False)[-1].route == "UI Automation"


def test_round80_tools_registered():
    from app.tools.catalog import build_default_registry

    reg = build_default_registry()
    for n in ("idle_work", "trace_export", "discover_integration"):
        assert reg.get(n) is not None


# ==== round 81 ==========================================================================================================
def test_lease_step_focus_only_for_automatic_leases(db):
    s = L.LeaseStore(db, Clock())
    L.ensure_auto(s, "m1", [("a", 2, False), ("b", 2, False)])
    s.focus("m1", "a")
    assert s.authorize("m1", "a", 0, False, {})[0]
    assert "not the step in progress" in s.authorize("m1", "b", 0, False, {})[1]
    s.focus("m1", "b")
    assert s.authorize("m1", "b", 0, False, {})[0]
    s.grant("m2", ["a", "b"])
    s.focus("m2", "a")                                                   # your own lease is not narrowed
    assert s.authorize("m2", "b", 0, False, {})[0]


def test_executor_predicts_changing_calls_only(db):
    from app.agent.tool_manager import ToolExecutor
    from app.tools.base import ToolResult

    ex = ToolExecutor.__new__(ToolExecutor)
    ex.context = type("C", (), {"database": db})()
    T0 = type("T", (), {"name": "read_file", "verify": None})()
    T1 = type("T", (), {"name": "write_file", "verify": None})()
    assert ex._predict(T0, "", 0) == ""                                  # read-only: no noise
    assert ex._predict(T1, "m1", 1) == ""                                # mission steps are predicted by the mission runner
    pid = ex._predict(T1, "", 1)
    assert pid
    ex._resolve_prediction(pid, ToolResult.failure("denied", "access denied"))
    p = P.Ledger(db).items(topic="tool:write_file")[0]
    assert p.outcome == "wrong" and "access denied" in p.evidence
    ex._resolve_prediction("", ToolResult.success("x"))                  # nothing to resolve: no error


def test_idle_speed_job_history_and_slowdown():
    from app.core import idle_work as I

    class Store(dict):
        def set(self, k, v):
            self[k] = v

    st = Store()
    S = type("S", (), {"ai_provider": "ollama", "ai_model": "jarvis", "ollama_host": "http://127.0.0.1:11434"})()
    speed = {"v": 40.0}
    job = I.speed_job(S, st, generate=lambda host, model: {"eval_count": int(speed["v"] * 10), "eval_duration": 10e9})
    for _ in range(5):
        assert "40.0 tokens/s" in job.run(None) and "SLOWER" not in job.run(None)
    speed["v"] = 8.0
    assert "SLOWER" in job.run(None)
    assert len(st[I.SPEED_KEY]) <= I.SPEED_KEEP
    assert "no speed was recorded" in I.speed_job(S, Store(), generate=lambda h, m: {}).run(None)
    assert I.speed_job(type("S", (), {"ai_provider": "foundry", "ai_model": "x"})(), st) is None
    assert I.speed_job(type("S", (), {"ai_provider": "ollama", "ai_model": ""})(), st) is None


def test_find_app_api_returns_unverified_leads(monkeypatch):
    from app.tools import browser, governance_tools as G
    from app.tools.base import ToolResult

    async def fake(args, ctx):
        assert "official API" in args.query
        return ToolResult.success("ok", results=[{"title": "Spotify Web API", "url": "https://developer.spotify.com"}])

    monkeypatch.setattr(browser, "web_search", fake)
    out = asyncio.run(G.find_app_api(G.ApiLeadArgs(app="spotify"), None))
    assert "not verified" in out.message and "developer.spotify.com" in out.message and out.data["note"] == browser.UNTRUSTED_NOTE

    async def none(args, ctx):
        return ToolResult.success("none", results=[])

    monkeypatch.setattr(browser, "web_search", none)
    assert "does not prove" in asyncio.run(G.find_app_api(G.ApiLeadArgs(app="x"), None)).message


def test_find_app_api_is_a_web_tool():
    from app.tools.catalog import build_default_registry

    t = build_default_registry().get("find_app_api")
    assert t.category == "web" and t.needs_network


# ==== round 82: review-first memory merge, grounding trial ==============================================================
def _mem(db):
    from app.memory.manager import MemoryManager

    return MemoryManager(db)


def test_merge_candidates_skip_private_and_different(db):
    from app.core import memory_consolidation as MC

    m = _mem(db)
    a = m.remember("My sister Rima lives in Dhaka near Gulshan", topic="t1")[0]
    b = m.remember("My sister Rima lives in Dhaka, near Gulshan lake", topic="t2")[0]
    m.remember("I prefer dark mode in every editor", topic="t3")[0]
    p = m.remember("My sister Rima lives in Dhaka near Gulshan area", private=True, topic="t4")[0]
    pairs = MC.candidates(m.all(limit=50, include_hidden=True))
    ids = [{x.id, y.id} for x, y, _ in pairs]
    assert {a.id, b.id} in ids and all(p.id not in s for s in ids)


def test_merge_model_wording_is_checked_for_invention(db):
    from app.core import memory_consolidation as MC

    m = _mem(db)
    a = m.remember("My sister Rima lives in Dhaka near Gulshan", topic="t5")[0]
    b = m.remember("My sister Rima lives in Dhaka, near Gulshan lake", topic="t6")[0]
    good = MC.propose(a, b, lambda prompt: "My sister Rima lives in Dhaka near Gulshan lake")
    assert good["by"] == "model" and "lake" in good["merged"]
    for bad in ("My sister Rima lives in Dhaka near Gulshan lake since 2019", "My sister Rima and Karim live in Dhaka near Gulshan lake", "", "Rima."):
        p = MC.propose(a, b, lambda prompt, bad=bad: bad)
        assert p["by"] == "rule" and p["merged"] == b.content and "rejected" in p["why"] or "no answer" in p["why"], bad

    def boom(prompt):
        raise RuntimeError("model down")

    assert MC.propose(a, b, boom)["by"] == "rule" and MC.propose(a, b, None)["why"] == "no model was used"


def test_merge_apply_undo_stale_and_reject(db):
    from app.core import memory_consolidation as MC
    from app.memory.settings_store import SettingsStore

    m, props = _mem(db), MC.Proposals(SettingsStore(db))
    a = m.remember("My sister Rima lives in Dhaka near Gulshan", topic="t7")[0]
    b = m.remember("My sister Rima lives in Dhaka, near Gulshan lake", topic="t8")[0]
    assert MC.scan(m, props) == 1 and MC.scan(m, props) == 0            # the same pair is not proposed twice
    prop = props.open()[0]
    pid, keep, off = prop["id"], prop["a_id"], prop["b_id"]
    assert {keep, off} == {a.id, b.id}
    msg = MC.apply(m, props, pid)
    assert "switched off" in msg and m.get(off).disabled and m.get(keep).content == b.content   # rule: the longer wording is kept
    MC.undo(m, props, pid)
    assert not m.get(off).disabled and {m.get(a.id).content, m.get(b.id).content} == {a.content, b.content}
    with pytest.raises(LookupError):
        MC.apply(m, props, pid)                                          # an undone proposal cannot be re-applied
    c = m.remember("The team meeting room is on the third floor east", topic="t9")[0]
    d = m.remember("The team meeting room is on the third floor, east side", topic="t10")[0]
    MC.scan(m, props)
    stale = [p for p in props.open() if {p["a_id"], p["b_id"]} == {c.id, d.id}][0]["id"]
    m.edit(m.get(c.id), "The team meeting room moved to the second floor")
    with pytest.raises(ValueError):
        MC.apply(m, props, stale)
    assert props.get(stale)["status"] == "stale" and not m.get(d.id).disabled and not m.get(c.id).disabled


def test_merge_tool_levels_and_idle_job(db):
    from app.core import idle_work as I
    from app.memory.settings_store import SettingsStore
    from app.security.permissions import RiskLevel
    from app.tools import governance_tools as G
    from app.tools.catalog import build_default_registry

    t = build_default_registry().get("memory_merge")
    assert t.level_for(G.MergeArgs(action="list"), None) == RiskLevel.SAFE and t.level_for(G.MergeArgs(action="apply", id="x"), None) == RiskLevel.MODERATE
    m = _mem(db)
    m.remember("My sister Rima lives in Dhaka near Gulshan", topic="t11")[0]
    m.remember("My sister Rima lives in Dhaka, near Gulshan lake", topic="t12")[0]
    st = SettingsStore(db)
    S = type("S", (), {"ai_provider": "none", "ai_model": ""})()
    assert "1 new merge proposal" in I.merge_job(S, st).run(db)
    assert all(not x.disabled for x in m.all(limit=10))                  # the idle job only proposes
    out = G.memory_merge(G.MergeArgs(action="list"), type("C", (), {"database": db})())
    assert "Nothing is changed until you say" in out.message


def test_grounding_parse_and_scoring():
    from app.validation import grounding_trial as GT

    assert GT.parse_point("(120, 40)", 800, 600) == (120.0, 40.0)
    assert GT.parse_point("x=0.5 y=0.25", 800, 600) == (400.0, 150.0)
    assert GT.parse_point("<think>maybe 3 4</think> (900, 500)", 800, 600) == (720.0, 300.0)      # a 0-1000 grid
    assert GT.parse_point("box [100, 20, 140, 60]", 800, 600) == (120.0, 40.0)
    assert GT.parse_point("I cannot see it", 800, 600) is None and GT.parse_point("(5000, 5000)", 800, 600) is None
    controls = [GT.Target("Save", (100, 20, 140, 60)), GT.Target("Open", (200, 20, 240, 60)), GT.Target("Close", (300, 20, 340, 60)),
                GT.Target("Close", (400, 20, 440, 60)), GT.Target("", (0, 0, 50, 50)), GT.Target("Tiny", (0, 0, 3, 3)), GT.Target("Help", (500, 20, 540, 60))]
    assert [t.name for t in GT.pick_targets(controls)] == ["Save", "Open", "Help"]                # ambiguous, unnamed and tiny ones are left out
    replies = {"Save": "(120, 40)", "Open": "(10, 10)", "Help": "no idea"}

    def ask(prompt):
        name = prompt.split('"')[1]
        return replies[name]

    t = GT.run("m", controls, (800, 600), ask)
    assert (t.targets, t.hits, t.no_coordinates) == (3, 1, 1) and "1 of 3" in GT.report(t) and "nothing was clicked" in GT.report(t)

    def boom(prompt):
        raise TimeoutError("slow")

    e = GT.run("m", controls, (800, 600), boom)
    assert e.hits == 0 and len(e.errors) == 3
    assert "No usable controls" in GT.report(GT.run("m", [], (800, 600), ask))
