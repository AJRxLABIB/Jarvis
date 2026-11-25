"""Round 93: the tool router trims big groups to the most relevant tools and offers a named tool outright."""

from app.agent import tool_router as TR
from app.tools.catalog import build_default_registry

R = build_default_registry()


def names(text, cap=None, recent=()):
    return [s.name for s in TR.select_specs(R, text, recent, max_tokens=cap)]


def tokens(text, cap=None):
    return sum(TR.spec_tokens(s) for s in TR.select_specs(R, text, max_tokens=cap))


def test_big_group_is_trimmed_but_keeps_the_relevant_tool():
    n = names("remember my favourite colour")
    assert "remember" in n and len(n) < 25 and tokens("remember my favourite colour") < 4000


def test_hinted_tools_are_offered_outside_their_group():
    assert "place_task" in names("which device should run this task")
    assert "ui_drift" in names("did the interface of excel change")
    assert "verify_claim" in names("did the save really work")
    assert "cognitive_plan" in names("how hard is this request", cap=2400)


def test_tight_budget_still_keeps_the_relevant_tool_not_the_first_in_registry_order():
    assert "investigate_problem" in names("why is my pc slow", cap=2400)


def test_recently_used_tool_survives_trimming():
    assert "lease" in names("remember my favourite colour", recent=("lease",))


def test_small_group_unchanged_and_relevance_scoring():
    spec = next(s for s in R.specs() if s.name == "remember")
    assert TR.relevance(TR._words("remember my colour"), spec) > TR.relevance(TR._words("weather forecast"), spec)
    small = R.specs()[:5]
    assert TR.rank_group(small, TR._words("anything")) == small
    assert TR.relevance(set(), spec) == 0.0


def test_no_named_group_still_offers_the_everyday_fallback():
    assert len(names("thanks")) > 0


def test_hints_for_requests_the_real_eval_got_wrong():
    from app.agent.tool_router import hinted

    assert "gesture_control" in hinted("Stop Finger Detection") and "gesture_control" in hinted("detect my finger")
    assert "jarvis_control" in hinted("restart yourself")
    assert "play_music" in hinted("play some music for me and put it on speaker")
