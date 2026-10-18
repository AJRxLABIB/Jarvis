from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.core import learning
from app.core.learning import LearningStore, overlap_quality, schedule
from app.tools.base import ToolContext, ToolError
from app.tools.knowledge import LearnArgs, learn
from tests.test_v2 import db


class Today:
    def __init__(self) -> None:
        self.day = date(2026, 10, 14)

    def __call__(self) -> date:
        return self.day


@pytest.fixture()
def world(tmp_path: Path):
    clock = Today()
    return LearningStore(db(tmp_path), today=clock), clock


def test_sm2_intervals_grow_and_failures_reset() -> None:
    ease, interval, reps = 2.5, 0, 0
    seen = []
    for _ in range(4):
        ease, interval, reps = schedule(ease, interval, reps, 5)
        seen.append(interval)
    assert seen[:2] == [1, 6] and seen[2] > 6 and seen[3] > seen[2]
    ease2, interval2, reps2 = schedule(ease, interval, reps, 1)
    assert (interval2, reps2) == (1, 0) and ease2 < ease and ease2 >= learning.MIN_EASE
    with pytest.raises(ToolError):
        schedule(2.5, 1, 1, 9)


def test_overlap_grading() -> None:
    assert overlap_quality("Paris", "It is Paris") == 5
    assert overlap_quality("Paris is the capital", "paris") == 2  # half of the key words: partial, not right
    assert overlap_quality("mitochondria produce ATP energy", "produce ATP energy") == 3
    assert overlap_quality("photosynthesis", "no idea") == 0
    assert overlap_quality("42", "42") == 5 and overlap_quality("42", "43") == 0


def test_reviews_schedule_and_explain(world) -> None:
    store, clock = world
    item = store.add("Geography", "Capital of France?", "Paris")
    assert store.add("geography", "capital of france?", "Paris") == item  # not duplicated
    assert [d["id"] for d in store.due()] == [item]
    good = store.review(item, "Paris")
    assert good["correct"] and good["graded_by"] == "word overlap" and good["next_in_days"] == 1
    assert store.due() == []
    clock.day += timedelta(days=1)
    assert [d["id"] for d in store.due()] == [item]
    bad = store.review(item, "London")
    assert not bad["correct"] and "The answer was: Paris" in bad["explain"] and bad["next_in_days"] == 1
    by_hand = store.review(item, "whatever", quality=4)
    assert by_hand["graded_by"] == "you" and by_hand["correct"]


def test_mastery_and_provenance_labels(world) -> None:
    store, _ = world
    a = store.add("Biology", "Powerhouse of the cell?", "Mitochondria", source="ai: made from my notes")
    store.add("Biology", "DNA stands for?", "Deoxyribonucleic acid", source="document: biology.pdf")
    store.review(a, "mitochondria")
    store.review(a, "nothing")
    row = store.mastery()[0]
    assert row["topic"] == "biology" and row["items"] == 2 and row["answered"] == 2 and row["correct_rate"] == 50
    bad = store.review(a, "x", quality=0)
    assert "written by the AI" in bad["explain"]
    assert "50%" in store.describe() or "33%" in store.describe()
    with pytest.raises(ToolError):
        store.add("t", "q", "a", source="the internet")
    assert store.forget_topic("Biology") == 2 and store.mastery() == []


def test_the_tool_flow(tmp_path: Path) -> None:
    ctx = ToolContext(Settings(), database=db(tmp_path), clock=lambda: datetime(2026, 10, 14, 9))
    added = learn(LearnArgs(action="add", topic="History", items=[{"question": "Year of independence?", "answer": "1971"}],
                            source="ai: from chapter 3"), ctx)
    assert "marked as written by the AI" in added.message
    quiz = learn(LearnArgs(action="quiz"), ctx)
    item_id = quiz.data["items"][0]["id"]
    assert "answer" not in quiz.data["items"][0]  # the quiz never shows the answer
    done = learn(LearnArgs(action="answer", item_id=item_id, given="1971"), ctx)
    assert done.data["correct"] and "Next review" in done.message
    assert learn(LearnArgs(action="quiz"), ctx).data["items"] == []
    assert "history" in learn(LearnArgs(action="progress"), ctx).message
    assert "Deleted 1" in learn(LearnArgs(action="forget_topic", topic="History"), ctx).message
    with pytest.raises(ToolError):
        learn(LearnArgs(action="add", topic="x"), ctx)
