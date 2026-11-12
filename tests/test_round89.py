import json

from app import longmem_eval as L


def test_builtin_cases_cover_the_question_types_and_run_without_a_model():
    cases = L.builtin()
    kinds = {c.kind for c in cases}
    assert {"single-session-user", "multi-session", "knowledge-update", "temporal-reasoning", "abstention"} <= kinds
    sc = L.run(cases)
    total = sum(v[1] for v in sc.retrieval.values())
    assert sc.cases == len(cases) and total == len(cases) - 1          # the abstention case has no retrieval target
    assert "abstention" not in sc.retrieval and sc.answers == {}
    text = L.report(sc, "built-in cases")
    assert "a floor, embeddings not used" in text and "not how good the memory is" in text and "official judge" not in text


def test_rank_uses_keywords_and_ignores_unrelated_sessions():
    sessions = {"a": "I moved to Chittagong last year.", "b": "Pasta recipe with garlic.", "c": "My office is in Chittagong near the port."}
    top = L.rank("Where in Chittagong is my office?", sessions)
    assert top[0] == "c" and "b" not in top
    assert L.rank("zzz qqq", sessions) == []


def test_answer_check_containment_and_abstention():
    c = L.Case("x", "single-session-user", "q", "golden retriever", {}, ["s"])
    assert L.answer_ok(c, "It is a Golden Retriever.") and not L.answer_ok(c, "A poodle.") and not L.answer_ok(c, "")
    a = L.Case("y_abs", "abstention", "q", "", {}, [])
    assert a.abstention and L.answer_ok(a, "I do not know - you did not mention that.") and not L.answer_ok(a, "Your cat is Tom.")


def test_run_with_model_counts_answers_and_skips_failures():
    cases = L.builtin()

    def ask(question, context):
        if "coffee" in question:
            raise TimeoutError("slow")
        if "cat" in question:
            return "I don't know."
        if "dog" in question:
            return "A golden retriever."
        return "no idea at all"

    sc = L.run(cases, ask)
    assert sc.skipped == 1 and sc.answers["abstention"] == [1, 1] and sc.answers["single-session-user"][0] == 1
    text = L.report(sc, "built-in cases", with_answers=True)
    assert "NOT the official judge" in text and "not counted" in text


def test_load_public_layout_and_skip_bad_rows(tmp_path):
    data = [{"question_id": "q1", "question_type": "multi-session", "question": "Where do I work?", "answer": "Fluxit",
             "haystack_session_ids": ["s1", "s2"], "haystack_sessions": [[{"role": "user", "content": "I work at Fluxit."}, {"role": "assistant", "content": "Noted."}],
                                                                        [{"role": "user", "content": "Nice weather."}]], "answer_session_ids": ["s1"]},
            {"question_id": "broken"}, "not an object"]
    p = tmp_path / "d.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    cases = L.load(p)
    assert len(cases) == 1 and cases[0].sessions["s1"].startswith("I work at Fluxit") and cases[0].answer_sessions == ["s1"]
    sc = L.run(cases)
    assert sc.retrieval == {"multi-session": [1, 1]}
    (tmp_path / "e.json").write_text("{}", encoding="utf-8")
    assert L.load(tmp_path / "e.json") == []


def test_analytics_panel_uses_real_tools():
    from app.tools.catalog import build_default_registry
    from app.ui.command_center import panel_specs as PS

    reg = build_default_registry()
    panel = PS.BY_KEY["analytics"]
    for block in panel.blocks:
        assert reg.get(block.tool) is not None, block.tool
    assert "not enough history" in panel.intro


def test_abstention_prompt_and_reply_detection():
    from app import longmem_eval as LM
    from app.ai.prompts import build_system_prompt  # noqa: F401

    assert "Never guess" in LM.SYSTEM
    case = [c for c in LM.builtin() if c.abstention][0]
    assert LM.answer_ok(case, "I don't know - that is not in the saved sessions.")
    assert not LM.answer_ok(case, "Your cat is called Max.")
