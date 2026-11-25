"""Round 92: reasoning levels + metacognition, InterBrain protocol + debate, experiment records. Pure logic; no model, network or Windows."""

import pytest

from app.core import cognition as C, experiments as X, interbrain as IB


# ------------------------------------------------------------------ cognition
def test_simple_question_is_level_1_or_2_and_needs_no_review():
    p = C.classify("what time is it")
    assert p.level <= 2 and not p.budget["review"] and p.brains[0] == "context"


def test_the_prompts_own_example_is_multi_objective_and_deep():
    p = C.classify("Check my project, find the main problems, fix the most important ones, and make sure nothing else breaks.")
    assert len(p.objectives) >= 3 and p.level >= 4
    assert p.budget["review"] and p.budget["evidence"]
    assert {"planning", "critic", "verification"} <= set(p.brains)


def test_risky_verb_raises_the_floor_and_adds_verification():
    p = C.classify("delete the old backups")
    assert p.level >= 3 and p.risky and "verification" in p.brains and "critic" in p.brains


def test_low_gpu_memory_and_power_mode_cap_the_level_and_say_so():
    text = "Investigate the crash, fix it, then make sure nothing else breaks. Analyze the logs thoroughly."
    free = C.classify(text)
    capped = C.classify(text, vram_free_gb=1.0)
    assert free.level >= 4 and capped.level == 3 and "GPU memory" in capped.capped_by
    assert C.classify(text, mode="low_resource").level == 2


def test_level_5_needs_real_breadth_or_an_explicit_request():
    assert C.classify("analyze the code").level < 5
    assert C.classify("Research the options, compare them, then plan the migration and make sure nothing breaks. Do this carefully.").level == 5


def test_clarify_only_when_the_ambiguity_matters():
    assert "Which one" in C.classify("delete it").clarification
    assert C.classify("delete it", known_referents=("report.docx",)).clarification == ""
    assert C.classify("what is it").clarification == ""           # reading: guessing is harmless


def test_intents_split_and_keep_order():
    parts = C.intents("open the notes; summarise them, then email the summary")
    assert parts == ["open the notes", "summarise them", "email the summary"]
    assert C.intents("") == []


def test_early_stopping_needs_enough_confident_agreeing_candidates():
    assert C.should_stop([0.9], 2)
    assert not C.should_stop([0.9], 4)
    assert not C.should_stop([0.5, 0.6], 3)
    assert C.should_stop([0.9, 0.85], 4, agree=2) and not C.should_stop([0.9, 0.85], 4, agree=1)


def test_premortem_flags_missing_verification_and_open_questions():
    p = C.classify("delete it")
    checks = {c.question: c for c in C.premortem(p)}
    assert not checks["Is a clarification needed?"].ok
    assert not checks["How will I verify the result?"].ok
    ok = {c.question: c for c in C.premortem(C.classify("delete the old backups"), evidence_have=True, verify_with="list the folder afterwards")}
    assert ok["How will I verify the result?"].ok and ok["Do I have enough information?"].ok


def test_postcheck_never_learns_from_unverified_success():
    a = C.postcheck(True, False, True, lesson="use method A")
    assert not a.learnable and any("NOT verified" in n for n in a.notes)
    assert C.postcheck(True, True, True, lesson="use method A").learnable
    assert not C.postcheck(False, True, False, lesson="x").learnable


# ------------------------------------------------------------------ interbrain
def msg(**kw):
    base = dict(task_id="t1", sender="research", recipient="reasoning", type="EVIDENCE_REPORT", findings=["found X"], evidence=["doc#1"])
    base.update(kw)
    return IB.Message(**base)


def test_protocol_rules():
    ok = IB.validate(msg())
    assert ok.id and ok.at
    for bad in (msg(type="CHAT"), msg(status="weird"), msg(confidence=1.5), msg(sender=""),
                msg(type="TASK_COMPLETED", evidence=[]), msg(type="CHALLENGE", findings=[], objective=""),
                msg(type="VERIFICATION_RESULT", status="pending")):
        with pytest.raises(IB.ProtocolError):
            IB.validate(bad)
    IB.validate(msg(type="TASK_COMPLETED", evidence=["file:/a"], status="done"))


def test_channel_is_bounded_and_tracks_open_challenges():
    ch = IB.Channel(limit=5)
    c = ch.post(msg(type="CHALLENGE", sender="critic", findings=["assumes the disk is local"]))
    assert ch.open_challenges("t1") == [c]
    ch.post(msg(type="CORRECTION", sender="reasoning", context_ref=c.id))
    assert ch.open_challenges("t1") == []
    for _ in range(10):
        ch.post(msg())
    assert len(ch.log) == 5 and "research -> reasoning: EVIDENCE_REPORT" in ch.timeline("t1")
    assert ch.timeline("nope") == "No messages for that task."


def test_debate_never_rewards_confidence_without_evidence():
    v = IB.debate([IB.Proposal("a", "answer A", [], 0.99), IB.Proposal("b", "answer B", ["retrieved"], 0.4)])
    assert v.winner.id == "b"
    none = IB.debate([IB.Proposal("a", "answer A", [], 0.99)])
    assert none.status == "insufficient" and none.winner is None


def test_debate_open_challenge_makes_it_contested_and_resolution_clears_it():
    props = [IB.Proposal("a", "restart the service", ["verified", "retrieved"], 0.8), IB.Proposal("b", "reinstall", ["inferred"], 0.9)]
    hard = IB.Challenge("a", "restarting loses unsaved work", 3)
    contested = IB.debate(props, [hard])
    assert contested.winner.id in ("a", "b") and (contested.status == "contested" or contested.winner.id == "b")
    clear = IB.debate(props, [IB.Challenge("a", "restarting loses unsaved work", 3, resolved=True)])
    assert clear.status == "clear" and clear.winner.id == "a"
    assert "Recommended" in clear.text()


# ------------------------------------------------------------------ experiments
class Mem:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def test_grading_never_overstates():
    st = X.Store(Mem())
    e = st.start("the extension slows startup", ["disable extension", "reboot"], baseline="42 s", expected="faster")
    assert st.conclude(e.id).strength == "open"
    st.observe(e.id, "30 s", True)
    assert st.conclude(e.id).strength == "correlation only"       # one observation and two variables
    one = st.start("the extension slows startup", ["disable extension"], baseline="42 s", expected="faster")
    st.observe(one.id, "30 s", True)
    st.observe(one.id, "31 s", True)
    assert st.conclude(one.id, "my note").strength == "supported"
    assert "not proof" in st.get(one.id).conclusion and st.get(one.id).conclusion.startswith("my note")
    nobase = st.start("x", ["a"])
    st.observe(nobase.id, "1", True)
    st.observe(nobase.id, "1", True)
    assert st.conclude(nobase.id).strength == "correlation only" and "no baseline" in st.get(nobase.id).conclusion


def test_contradicted_inconclusive_delete_and_cap():
    st = X.Store(Mem())
    a = st.start("h", ["v"], baseline="b")
    st.observe(a.id, "same", False)
    assert st.conclude(a.id).strength == "contradicted"
    st.observe(a.id, "better", True)
    assert st.conclude(a.id).strength == "inconclusive"
    assert st.remove(a.id) and not st.remove(a.id) and st.observe(999, "x", True) is None
    for i in range(X.MAX + 5):
        st.start(f"h{i}", ["v"])
    assert len(st.all()) == X.MAX and "h" in X.describe(st.all()[-1])


def test_tools_run():
    import asyncio
    from types import SimpleNamespace as NS

    from app.tools.cognition_tools import PlanArgs, cognitive_plan

    r = asyncio.run(cognitive_plan(PlanArgs(request="fix the bug, then run the tests"), NS()))
    assert r.data["level"] >= 2 and "advisory" in r.message


def test_reasoning_routing_is_opt_in_and_only_raises_the_role():
    from types import SimpleNamespace as NS

    from app.agent.conversation import ConversationManager as CM
    from app.config.settings import Settings

    assert Settings().reasoning_routing is False

    def routed(text, on):
        me = NS(_current_text=text, settings=NS(reasoning_routing=on, performance_mode="balanced"), _apply_performance=lambda d: d, last_route=None)
        return CM._route(me).kind

    risky = "delete the old backups"                       # short, so the plain router calls it simple -> fast
    assert routed(risky, False) == "fast"
    assert routed(risky, True) == "smart"                  # level 3 (costly if wrong) -> the bigger model
    assert routed("what time is it", True) == routed("what time is it", False) == "fast"


def test_live_blackboard_leaves_a_typed_interbrain_record():
    from app.core import blackboard as B

    bb = B.Blackboard("run1")
    t = bb.begin("developer", "run_tests", {"x": 1}, mutating=False)
    bb.finish(t, True, "12 passed")
    t2 = bb.begin("developer", "git_commit", {"m": "a"}, mutating=True)
    bb.finish(t2, False, "hook failed")
    bb.claim("reviewer", "the change is small", "inferred")
    bb.claim("verifier", "tests pass", "verified")
    bb.need("planner", "which branch?")
    types = [m.type for m in bb.channel.for_task("run1")]
    assert types == ["TASK_ACCEPTED", "EVIDENCE_REPORT", "TASK_ACCEPTED", "FAILURE_REPORT", "HYPOTHESIS", "EVIDENCE_REPORT", "DEPENDENCY_UPDATE"]
    assert all(m.recipient == "supervisor" for m in bb.channel.log)
    assert "developer -> supervisor: FAILURE_REPORT" in bb.timeline() and bb.view()["messages"]
    assert B.for_run("zz").task_id == "zz"


def test_supervisor_decision_is_structured_and_model_json_is_untrusted():
    d = C.classify("delete the old backups").decision()
    assert d["execution_mode"] == "approval_required" and d["verification_required"] and d["mode"] in C.MODES.values()
    assert C.validate_decision(d) == d
    for bad in ({**d, "mode": "God"}, {**d, "extra": 1}, {k: v for k, v in d.items() if k != "goal"}, {**d, "requires_tools": "yes"}, {**d, "roles": [1]}, "text", None):
        with pytest.raises(ValueError):
            C.validate_decision(bad)
    lax = {**d, "execution_mode": "direct", "verification_required": False, "mode": "Quick"}
    safe = C.at_least(lax, d)
    assert safe["execution_mode"] == "approval_required" and safe["verification_required"] and safe["mode"] == d["mode"]
    assert C.classify("delete it").decision()["execution_mode"] == "clarify_first"
    assert C.classify("what time is it").decision()["execution_mode"] == "direct"
