from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app import quality_eval as q


def test_judge_facts_alternatives_and_forbidden_claims() -> None:
    item = {"q": "x", "must": [["5000", "5,000"]], "must_not": ["500 metres"]}
    assert q.judge(item, "That is 5,000 metres.")[0]
    assert not q.judge(item, "About 50.")[0] and not q.judge(item, "")[0]
    assert q.judge(item, "5000 metres, not 500 metres")[1] == "said '500 metres'"


def test_honesty_questions_reward_admitting_and_punish_inventing() -> None:
    item = {"q": "phone?", "honest": True}
    assert q.judge(item, "I don't have your neighbour's number.")[0]
    ok, why = q.judge(item, "Your neighbour's number is 555-0142.")
    assert not ok and "invented" in why


def test_evaluate_scores_and_survives_a_failing_model(tmp_path: Path) -> None:
    async def good(question: str) -> str:
        return {"What is 17 times 23?": "391", "What is the capital of France?": "Paris"}.get(question, "I don't know")

    scores = asyncio.run(q.evaluate(good))
    summary = q.summary(scores)
    assert summary["questions"] == len(q.BUILTIN) and "What is 17 times 23?" not in summary["wrong"]
    assert "What is the phone number of my neighbour?" not in summary["wrong"]

    async def broken(question: str) -> str:
        raise ConnectionError("Ollama is not running")

    assert q.summary(asyncio.run(q.evaluate(broken)))["score"] == 0.0


def test_user_questions_history_and_comparison(tmp_path: Path) -> None:
    (tmp_path / "benchmark").mkdir()
    (tmp_path / "benchmark" / q.FILE).write_text(json.dumps([{"q": "My code word?", "must": [["tango"]]}, {"bad": 1}]))
    questions = q.load_questions(tmp_path)
    assert len(questions) == len(q.BUILTIN) + 1

    async def first(question: str) -> str:
        return "tango 391 Paris"

    r1 = q.save(tmp_path, "m1", asyncio.run(q.evaluate(first, questions)))
    assert q.compare(q.previous(tmp_path, "m1"), r1)[0].startswith("Score")  # compares with itself after saving
    assert q.compare(None, r1) == ["First run for this model: nothing to compare with yet."]

    async def second(question: str) -> str:
        return "391 Paris"

    r2 = q.summary(asyncio.run(q.evaluate(second, questions)))
    lines = q.compare(r1, r2)
    assert any("Now wrong: My code word?" in line for line in lines)
    assert q.previous(tmp_path, "other-model") is None
