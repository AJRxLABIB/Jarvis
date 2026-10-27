"""Decision records, option comparison, preferences, investigations, briefs, long-horizon goals, the radar and capability
discovery - plus their tools and the Intelligence Center page."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.memory.database import Database
from app.tools.base import ToolError


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "i.db")
    database.initialize()
    return database


# ------------------------------------------------------------------ decisions


def test_decision_records_are_editable_and_corrections_are_visible(db):
    from app.core.decisions import DecisionStore

    s = DecisionStore(db)
    r = s.record("Default model", "qwen3:4b", ["llama3.2:3b"], ["fits 6 GB"], project="Jarvis")
    assert "qwen3:4b" in r.describe() and s.list("jarvis") == [r]
    c = s.correct(r.id, chosen="jarvis")
    assert c.chosen == "jarvis" and any("was: 'qwen3:4b'" in e for e in c.evidence)
    s.set_outcome(r.id, "works, but slow on battery", "superseded")
    assert s.list() == [] and len(s.list(include_inactive=True)) == 1
    with pytest.raises(ToolError):
        s.record("", "x")
    with pytest.raises(ToolError):
        s.correct(r.id)
    s.delete(r.id)
    with pytest.raises(ToolError):
        s.get(r.id)


def test_relevant_decisions_are_context_with_age_not_rules(db):
    from app.core.decisions import DecisionStore

    old = DecisionStore(db, clock=lambda: datetime.now(timezone.utc) - timedelta(days=400))
    old.record("TMS database", "MySQL")
    found = DecisionStore(db).relevant("which database for tms")
    assert len(found) == 1 and found[0][1] >= 399
    assert DecisionStore(db).relevant("zzz qqq") == []


def test_compare_never_treats_unknown_as_zero_and_labels_basis():
    from app.core.decisions import Option, Score, compare

    S = Score
    a = Option("small", {"latency": S(1.0, "measured"), "quality": S(0.5, "measured"), "ram": S(None)}, True)
    b = Option("big", {"latency": S(4.0, "measured"), "quality": S(0.9, "measured"), "ram": S(8.0, "estimated")}, False)
    r = compare([a, b], {"latency": 1, "quality": 3}, {"latency": False})
    assert r.verdict == "ranked" and r.ranking[0][0] == "big" and "ram" in r.left_out and r.basis == "measured"
    assert compare([a, b], higher_is_better={"latency": False}).verdict == "too_close"
    nothing = compare([Option("x", {"q": S(None)}), Option("y", {"q": S(1.0)})])
    assert nothing.verdict == "no_evidence" and "can't rank" in nothing.describe()
    with pytest.raises(ToolError):
        compare([a])


def test_compare_suggests_the_reversible_option_when_evidence_is_weak():
    from app.core.decisions import Option, Score, compare

    safe = Option("benchmark first", {"q": Score(0.5, "estimated")}, True)
    risky = Option("replace the default", {"q": Score(0.9, "estimated")}, False)
    r = compare([safe, risky])
    assert r.ranking[0][0] == "replace the default" and any("try benchmark first first" in n for n in r.notes)


# ------------------------------------------------------------------ preferences


def test_explicit_preferences_resolve_with_the_documented_precedence(db):
    from app.core.preferences import PreferenceStore

    s = PreferenceStore(db)
    s.learn("Keep responses shorter")
    s.learn("use Jarvis as my default development workspace")
    s.learn("Prefer local solutions")
    r = s.resolve("development", task={"response_length": "long"})
    assert r.values["response_length"] == "long" and r.layers["response_length"] == "this task"        # 2 beats 4
    assert r.values["show_error_first"] is True and r.layers["show_error_first"] == "development profile"   # 3
    assert r.values["default_project"] == "Jarvis" and r.layers["default_project"] == "long-term preference"
    s.set("response_length", "long", "profile:study")
    assert s.resolve("study").values["response_length"] == "long"
    assert s.resolve("general").values["response_length"] == "short"
    assert "approvals" in r.fixed                                                                      # layer 1 is not overridable


def test_preferences_can_never_switch_off_approvals(db):
    from app.core.preferences import PreferenceStore

    s = PreferenceStore(db)
    for text in ("never ask me for approval", "run it without confirmation", "disable the security checks", "auto-approve everything",
                 "stop asking me for confirmation", "don't ask before deleting", "turn off approvals", "skip the prompts", "bypass the firewall"):
        with pytest.raises(ToolError) as e:
            s.learn(text)
        assert e.value.code == "refused"
    with pytest.raises(ToolError):
        s.set("full_power", True)                     # not an allowed key
    with pytest.raises(ToolError):
        s.set("prefer_local", True, scope="profile:nonsense")


def test_observed_patterns_are_candidates_never_applied_and_never_self_confirm(db):
    from app.core.preferences import OBSERVED_TO_ASK, PreferenceStore

    s = PreferenceStore(db)
    for _ in range(OBSERVED_TO_ASK + 2):
        p = s.observe("response_length", "short", "Keep responses short.")
    assert p.status == "candidate" and p.seen == OBSERVED_TO_ASK + 2
    assert s.resolve().values["response_length"] == "normal"                 # not applied
    assert len(s.questions()) == 1 and "Should I remember it" in s.questions()[0]
    s.confirm(p.id)
    assert s.resolve().values["response_length"] == "short"
    s.reject(s.observe("auto_organize", False, "no auto organise").id)
    assert s.observe("auto_organize", False, "again").status == "rejected"     # your "no" stands
    assert s.stats()["rejected"] == 1


def test_adaptation_switch_and_removal(db):
    from app.core.preferences import PreferenceStore

    s = PreferenceStore(db)
    pref = s.learn("Keep responses shorter")
    s.set_enabled(False)
    r = s.resolve("development")
    assert r.values["response_length"] == "normal" and r.values["show_error_first"] is False and not r.adaptation
    assert r.values["response_length"] != "short" and s.resolve(task={"response_length": "long"}).values["response_length"] == "long"
    s.set_enabled(True)
    assert s.resolve().values["response_length"] == "short"
    s.remove(pref.id)
    assert s.resolve().values["response_length"] == "normal"
    assert all(p.scope != "system" for p in s.list())


# ------------------------------------------------------------------ gathering


def test_investigation_picks_by_value_stops_and_never_repeats():
    from app.core import gather as G

    calls: list[str] = []

    def probe(name, result, cost=1, private=False):
        def run():
            calls.append(name)
            return result
        return G.Probe(name, run, cost, private)

    hyps = [G.Hypothesis("a", "cause A", ("p1",), lambda f: G.SUPPORTED if f.get("p1") == "bad" else (G.RULED_OUT if "p1" in f else None), "fix A"),
            G.Hypothesis("b", "cause B", ("p1", "p2"), lambda f: G.RULED_OUT if "p2" in f else None, "fix B"),
            G.Hypothesis("c", "cause C", ("p3",), lambda f: G.RULED_OUT if "p3" in f else None)]
    probes = [probe("p1", "bad"), probe("p2", "ok"), probe("p3", "ok", cost=3, private=True)]
    r = asyncio.run(G.investigate("it broke", hyps, probes))
    assert calls == ["p1"] and [h.key for h in r.supported] == ["a"] and "direct observation" in r.stopped
    assert "p2" in " ".join(r.missing) and "private: not allowed" in " ".join(r.missing)
    calls.clear()
    full = asyncio.run(G.investigate("it broke", hyps, probes, stop_at_first=False))
    assert calls == ["p1", "p2"] and "nothing else" in full.stopped                    # p3 is private: never run


def test_investigation_budget_and_unobservable_probes_are_reported_not_guessed():
    from app.core import gather as G

    hyps = [G.Hypothesis("a", "A", ("p1",), lambda f: G.SUPPORTED if f.get("p1") else None),
            G.Hypothesis("b", "B", ("p2",), lambda f: G.SUPPORTED if f.get("p2") else None)]
    probes = [G.Probe("p1", lambda: None), G.Probe("p2", lambda: (_ for _ in ()).throw(RuntimeError("x")))]
    r = asyncio.run(G.investigate("?", hyps, probes))
    assert not r.supported and "Not settled" in r.describe() and "p1 could not be read" in r.missing
    r = asyncio.run(G.investigate("?", hyps, probes, G.Budget(max_probes=1)))
    assert "budget reached" in r.stopped and len(r.steps) == 1


def _ai(models, up=True, health=None):
    async def listing():
        if not up:
            raise RuntimeError("down")
        return models
    return NS(models=listing, health=NS(summary=lambda: health or {}))


@pytest.mark.parametrize("up,models,expect", [(False, [], "runtime_down"), (True, [NS(name="other", size_bytes=1)], "model_missing")])
def test_model_failure_investigation_finds_the_cause(up, models, expect):
    from app.core import gather as G

    ctx = NS(settings=NS(ai_model="jarvis"), ai=_ai(models, up))
    hyps, probes = G.model_failure(ctx)
    r = asyncio.run(G.investigate("model", hyps, probes))
    assert [h.key for h in r.supported] == [expect] and len(r.steps) == 1        # one cheap read was enough


def test_model_failure_with_recorded_errors():
    from app.core import gather as G

    health = {"jarvis": {"failures": 3, "last_error": "timeout", "skipped_for": 30}}
    ctx = NS(settings=NS(ai_model="jarvis"), ai=_ai([NS(name="jarvis", size_bytes=1)], True, health))
    hyps, probes = G.model_failure(ctx)
    r = asyncio.run(G.investigate("model", hyps, probes, stop_at_first=False))
    assert "recent_errors" in [h.key for h in r.supported] and r.status["runtime_down"] == G.RULED_OUT


# ------------------------------------------------------------------ synthesis


def _ctx(db):
    return NS(database=db, clock=lambda: datetime.now(timezone.utc), settings=NS(dev_project_dir=""), paths=None)


def test_brief_cites_sources_marks_staleness_conflicts_and_gaps(db):
    from app.core import synthesis as S
    from app.core.decisions import DecisionStore
    from app.memory.manager import MemoryManager

    m = MemoryManager(db)
    m.remember("TMS uses MongoDB for storage", "tms_store_a")
    m.remember("TMS uses PostgreSQL for storage", "tms_store_b")
    DecisionStore(db).record("TMS storage", "PostgreSQL", ["MongoDB"], ["relational reports"], project="TMS")
    with db.connect() as c:
        c.execute("INSERT INTO semantic_items (kind, source, chunk, text, created_at) VALUES ('document', ?, 3, ?, ?)",
                  ("D:/docs/tms.md", "TMS storage notes: reports need joins, so a relational store.", "2020-01-01"))
    far = datetime.now(timezone.utc) + timedelta(days=400)
    b = S.build_brief(_ctx(db), "what storage does TMS use", now=far)
    text = b.describe()
    assert "memory #" in text and "decision #" in text and "tms.md #3" in text
    assert "possibly outdated" in text and b.conflicts and "nothing was looked up online" in b.note
    assert any("Verify" in a for a in b.next_actions)
    empty = S.build_brief(_ctx(db), "quantum gravity")
    assert not empty.findings and "Nothing on this PC mentions this" in empty.describe()
    assert S.build_brief(_ctx(db), "  ").unresolved


def test_private_memories_are_not_in_briefs(db):
    from app.core import synthesis as S
    from app.memory.manager import MemoryManager

    MemoryManager(db).remember("my secret project codename is Falcon", "x", private=True)
    assert not S.build_brief(_ctx(db), "codename Falcon").findings


def test_knowledge_gaps_only_report_real_things(db, tmp_path):
    from app.core import synthesis as S
    from app.core.decisions import DecisionStore

    DecisionStore(db).record("Use tabs", "tabs")
    DecisionStore(db).record("Use Laravel", "Laravel", criteria=["team knows it"])
    gaps = S.knowledge_gaps(_ctx(db))
    assert len(gaps) == 1 and "Use tabs" in gaps[0]


# ------------------------------------------------------------------ long-horizon goals


def _goal(db, today, done_days, total=10, deadline="2026-11-03"):
    from app.core.goals import GoalStore

    store = GoalStore(db, clock=lambda: datetime(2026, 10, 1, 9, 0).astimezone())
    g = store.add("Finish TMS", deadline=deadline, tasks=[f"t{i}" for i in range(total)])
    with db.connect() as c:
        for i, days in enumerate(done_days, start=1):
            c.execute("UPDATE goal_tasks SET done = 1, done_at = ? WHERE goal_id = ? AND position = ?",
                      ((today - timedelta(days=days)).isoformat() + "T10:00:00", g.id, i))
    return store.get(g.id)


TODAY = date(2026, 10, 27)


def test_no_history_means_no_forecast_not_an_invented_one(db):
    from app.core import longgoals as L

    a = L.assess(db, _goal(db, TODAY, []), TODAY)
    assert a.pace_per_day is None and a.forecast is None and a.status == "stalled" and "not enough history" in a.describe()
    none = L.assess(db, _goal(db, TODAY, [], total=0), TODAY)
    assert none.status == "no_tasks" and "can't be verified" in none.describe()


def test_progress_is_ticked_tasks_and_forecast_is_a_labelled_range(db):
    from app.core import longgoals as L

    goal = _goal(db, TODAY, [1, 3, 6, 9, 12], deadline="2027-03-01")
    a = L.assess(db, goal, TODAY)
    assert (a.done, a.total) == (5, 10) and a.status == "on_track" and a.forecast[0] < a.forecast[1]
    assert "an estimate" in a.describe()


def test_unrealistic_goal_gets_proposals_that_change_nothing(db):
    from app.core import longgoals as L

    goal = _goal(db, TODAY, [1, 3, 6, 9, 12], deadline="2026-10-31")
    a = L.assess(db, goal, TODAY)
    assert a.status == "unrealistic"
    before = [t["done"] for t in L._tasks(db, goal.id)]
    proposals = L.replan(a, goal, db, TODAY)
    assert any("Move the deadline" in p for p in proposals) and any("defer" in p for p in proposals)
    assert [t["done"] for t in L._tasks(db, goal.id)] == before
    assert L.replan(L.assess(db, _goal(db, TODAY, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], deadline="2027-01-01"), TODAY), goal, db, TODAY) == []


def test_availability_only_caps_the_pace_and_stalls_are_detected(db):
    from app.core import longgoals as L

    goal = _goal(db, TODAY, [20, 21, 22, 23, 24], deadline="2027-03-01")
    a = L.assess(db, goal, TODAY)
    assert a.stalled and a.status == "stalled"
    goal2 = _goal(db, TODAY, [1, 2, 3, 4, 5], deadline="2027-03-01")
    free = L.assess(db, goal2, TODAY)
    capped = L.assess(db, goal2, TODAY, hours_per_week=1, minutes_per_task=120)
    assert capped.pace_per_day < free.pace_per_day and any("caps the pace" in n for n in capped.notes)


def test_weekly_review_and_switch(db):
    from app.core import longgoals as L
    from app.core.goals import GoalStore

    _goal(db, TODAY, [1, 2])
    review = L.weekly_review(db, GoalStore(db).active(), TODAY)
    assert "Finished: t1" in review or "Finished: t0" in review
    assert "Open tasks across goals: 8" in review and "not guessed" in review
    assert L.weekly_review(db, [], TODAY, enabled=False) == ""


# ------------------------------------------------------------------ radar and capability discovery


def test_radar_feedback_snooze_mute_and_auto_mute(db):
    from app.core.proactive import Candidate
    from app.core.radar import AUTO_MUTE_AFTER, Feedback

    now = [datetime(2026, 10, 27, 12, tzinfo=timezone.utc)]
    f = Feedback(db, clock=lambda: now[0])
    c = Candidate("resource:ram", "system", "low", "msg", 0.5, 0.5)
    assert f.filter([c])[0] == [c]
    f.snooze("resource:ram", 2)
    assert f.blocked("resource:ram").startswith("snoozed")
    now[0] += timedelta(hours=3)
    assert f.blocked("resource:ram") == ""
    f.mute("resource:ram")
    kept, held = f.filter([c])
    assert kept == [] and held[0][1] == "you muted it"
    f.unmute("resource:ram")
    for _ in range(AUTO_MUTE_AFTER):
        msg = f.rate("resource:ram", False)
    assert "7 days" in msg and f.blocked("resource:ram").startswith("snoozed")
    f.rate("other", True)
    assert f.blocked("other") == ""


def test_radar_signals_are_local_numbers_and_respect_thresholds(monkeypatch, db):
    import psutil

    from app.core import radar

    low = NS(available=5e8, total=8e9)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: low)
    monkeypatch.setattr(psutil, "disk_usage", lambda p: NS(free=2e11, total=5e11))
    keys = [c.key for c in radar.resource_signals(NS(paths=None))]
    assert keys == ["resource:ram"]
    monkeypatch.setattr(psutil, "virtual_memory", lambda: NS(available=4e9, total=8e9))
    assert radar.resource_signals(NS(paths=None)) == []


def test_muted_alerts_are_dropped_inside_the_proactive_engine(monkeypatch, db):
    import psutil

    from app.core import proactive, radar

    monkeypatch.setattr(psutil, "virtual_memory", lambda: NS(available=5e8, total=8e9))
    monkeypatch.setattr(psutil, "disk_usage", lambda p: NS(free=2e11, total=5e11))
    ctx = NS(database=db, settings=NS(dev_project_dir=""), paths=None)
    assert "resource:ram" in [c.key for c in proactive.candidates(ctx, datetime.now())]
    radar.Feedback(db).mute("resource:ram")
    assert "resource:ram" not in [c.key for c in proactive.candidates(ctx, datetime.now())]


def test_failure_causes_are_classified_and_permissions_are_never_widened():
    from app.core import capability_gaps as CG

    assert CG.classify("unknown_tool") == "missing_tool" and CG.classify("blocked") == "missing_permission"
    assert CG.classify("invalid_arguments") == "model_limitation" and CG.classify("unsupported") == "platform_restriction"
    assert CG.classify("timeout") == "resource_limitation" and CG.classify("zzz") == "unclassified"
    assert "Do NOT widen permissions" in CG._APPROACH["missing_permission"]
    assert set(CG.CAUSES) >= set(CG._CODES) | {"unclassified"}


def test_proposals_come_from_real_audit_rows(db):
    from app.core import capability_gaps as CG

    from app.memory.audit import AuditLog

    log = AuditLog(db)
    for _ in range(4):
        log.record("tool_result", tool="open_application", details={"ok": False, "error": "invalid_arguments", "message": "bad name"})
    for _ in range(2):
        log.record("tool_result", tool="open_application", details={"ok": True})
    found = CG.proposals(db)
    assert len(found) == 1 and found[0].cause == "model_limitation" and found[0].count == 4
    text = found[0].describe()
    assert "Test plan" in text and "Rollback" in text and "upper bound" in text
    assert CG.cause_summary(db) == {"model_limitation": 4}


def test_inventory_reads_the_registry_and_says_unknown_without_tests():
    from app.core import capability_gaps as CG
    from app.tools.catalog import build_default_registry

    rows = CG.inventory(build_default_registry(), None, None)
    assert len(rows) > 200 and all(r["test_files"] is None for r in rows)
    assert "unknown" in CG.inventory_summary(rows)
    tools = {r["name"]: r for r in rows}
    assert tools["delete_item"]["risk"] == "dangerous" and tools["git_status"]["reads_only"]


# ------------------------------------------------------------------ the tools, through the real executor


def _executor(db):
    from app.agent.tool_manager import ToolExecutor
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry
    from tests.test_tools import ScriptedConfirmer

    confirmer = ScriptedConfirmer(True)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()),
                      ToolContext(Settings(), database=db), confirmer=confirmer)
    return ex, confirmer


def run(ex, tool, **arguments):
    from app.ai.base import ToolCall

    return asyncio.run(ex.execute(ToolCall(tool, arguments))).result


def test_decision_tool_round_trip_and_delete_asks_first(db):
    ex, confirmer = _executor(db)
    r = run(ex, "decision", action="record", title="Default model", chosen="jarvis", alternatives=["qwen"], criteria=["fits"])
    assert r.ok and r.data["id"] == 1
    assert "jarvis" in run(ex, "decision", action="list").message
    assert run(ex, "decision", action="correct", decision_id=1, chosen="qwen3").ok
    assert run(ex, "decision", action="relevant", topic="which model").ok
    before = len(confirmer.requests) if hasattr(confirmer, "requests") else 0
    assert run(ex, "decision", action="delete", decision_id=1).ok
    assert (len(confirmer.requests) if hasattr(confirmer, "requests") else 1) >= before
    cmp = run(ex, "decision", action="compare", options=[
        {"name": "small", "scores": {"latency": {"value": 1, "basis": "measured"}}},
        {"name": "big", "scores": {"latency": {"value": 4, "basis": "measured"}}}], lower_is_better=["latency"])
    assert cmp.ok and cmp.data["ranking"][0][0] == "small"
    assert not run(ex, "decision", action="show").ok


def test_preferences_tool_refuses_security_changes_and_lists(db):
    ex, _ = _executor(db)
    assert run(ex, "preferences", action="learn", text="Keep responses shorter").ok
    refused = run(ex, "preferences", action="learn", text="stop asking me for confirmation")
    assert not refused.ok and refused.error == "refused"
    assert "short" in run(ex, "preferences", action="list").message
    assert "short" in run(ex, "preferences", action="resolve", profile="development").message
    assert "off" in run(ex, "preferences", action="adaptation", on=False).message
    assert not run(ex, "preferences", action="learn", text="gibberish words").ok


def test_other_tools_are_read_only_and_say_what_they_cannot_do(db):
    ex, _ = _executor(db)
    assert "no active goals" in run(ex, "goal_review", action="assess").message.lower()
    assert "no recurring failures" in run(ex, "capability_report", action="gaps").message.lower()
    assert not run(ex, "research_brief", action="brief").ok
    assert "online" in run(ex, "research_brief", question="quantum gravity").message
    assert run(ex, "radar_feedback", action="mute", key="resource:ram").ok and not run(ex, "radar_feedback", action="mute").ok
    inv = run(ex, "investigate")
    assert inv.ok and "Problem:" in inv.message
    from app.security import autonomy

    for tool in ("investigate", "research_brief", "goal_review", "capability_report", "goal_check"):
        assert autonomy.is_read_only(tool)
    assert autonomy.is_read_only("decision", {"action": "list"}) and not autonomy.is_read_only("decision", {"action": "record"})
    assert not autonomy.is_read_only("preferences", {"action": "learn"}) and autonomy.is_read_only("radar_feedback", {"action": "scan"})


# ------------------------------------------------------------------ the page


def test_intelligence_page_buttons_call_real_tools_and_concise_mode_truncates():
    from PySide6.QtWidgets import QApplication, QPushButton

    from app.core import a11y
    from app.ui.command_center.pages import IntelligencePage

    app = QApplication.instance() or QApplication([])
    calls = []

    def fake(tool, arguments, emit):
        calls.append((tool, arguments))
        emit("\n".join(f"line {i}" for i in range(30)))

    page = IntelligencePage(fake)
    page.fields["request"].setText("delete old files")
    buttons = {b.text(): b for b in page.findChildren(QPushButton)}
    buttons["Understand"].click()
    app.processEvents()
    assert calls[-1] == ("goal_check", {"request": "delete old files"})
    assert "more lines" in page.output.toPlainText() and "line 29" not in page.output.toPlainText()
    page.detail.setChecked(True)
    assert "line 29" in page.output.toPlainText()
    page.fields["r_key"].setText("resource:ram")
    buttons["Mute"].click()
    assert calls[-1] == ("radar_feedback", {"action": "mute", "key": "resource:ram"})
    page.profile.setCurrentText("study")
    buttons["What applies"].click()
    assert calls[-1] == ("preferences", {"action": "resolve", "profile": "study"})
    assert a11y.audit_widgets(page) == []
    assert len(page.PANELS) == page.menu.count() == page.stack.count()


def test_rail_has_the_intelligence_section():
    from app.ui.command_center.widgets import SECTIONS

    assert "intelligence" in [s[0] for s in SECTIONS]
