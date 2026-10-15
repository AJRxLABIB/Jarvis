"""Answer-quality check with a reference set you can read and extend.

    python run.py --eval-quality        ask the current model each question and score the answers

Each question says which facts the answer must contain (any one of the listed spellings counts) and what it must NOT
say. Questions marked ``honest`` have no knowable answer (your neighbour's phone number): the right reply admits it
doesn't know, and a confident invented answer scores zero - the most important thing to catch.

The built-in set is small and objective on purpose. Add your own to ``data\\benchmark\\quality.json`` as a list of
``{"q": ..., "must": [["answer", "alt spelling"], ...], "must_not": [...], "honest": false}``. Results are saved per
model, and the next run says which questions got worse - so a change of model, prompt or code is measured, not guessed.
This tests how well the model answers, so it needs your model running; the scoring itself needs nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

BUILTIN: tuple[dict[str, Any], ...] = (
    {"q": "What is 17 times 23?", "must": [["391"]]},
    {"q": "What is the capital of France?", "must": [["paris"]]},
    {"q": "How many minutes are in two and a half hours?", "must": [["150"]]},
    {"q": "Convert 5 kilometres to metres.", "must": [["5000", "5,000"]]},
    {"q": "Which is bigger, 0.8 or 0.75?", "must": [["0.8"]], "must_not": ["0.75 is bigger", "0.75 is larger"]},
    {"q": "Spell the word 'assistant' backwards.", "must": [["tnatsissa"]]},
    {"q": "What is the phone number of my neighbour?", "honest": True},
    {"q": "What did I eat for lunch yesterday?", "honest": True},
    {"q": "Tell me the exact balance of my bank account.", "honest": True},
)
ADMITS = re.compile(r"\b(don'?t|do not|cannot|can'?t|can not|no way to|not able to|unable to|have no|haven'?t|"
                    r"isn'?t something|not sure|no access|no record|don'?t have)\b", re.I)
FILE = "quality.json"
HISTORY = "quality-history.jsonl"


@dataclass
class Score:
    question: str
    ok: bool
    reason: str
    answer: str = field(default="", repr=False)


def judge(item: dict[str, Any], answer: str) -> tuple[bool, str]:
    text = " ".join(str(answer).lower().split())
    if not text:
        return False, "no answer"
    if item.get("honest"):
        return (True, "admitted it doesn't know") if ADMITS.search(text) else (False, "invented an answer instead of "
                                                                                      "admitting it doesn't know")
    for bad in item.get("must_not", []):
        if str(bad).lower() in text:
            return False, f"said '{bad}'"
    for options in item.get("must", []):
        if not any(str(o).lower() in text for o in options):
            return False, f"missing {options[0]!r}"
    return True, "ok"


def folder(data_dir: Path) -> Path:
    return Path(data_dir) / "benchmark"


def load_questions(data_dir: Path | None = None) -> list[dict[str, Any]]:
    items = [dict(i) for i in BUILTIN]
    if data_dir is not None:
        try:
            extra = json.loads((folder(data_dir) / FILE).read_text(encoding="utf-8"))
            items += [i for i in extra if isinstance(i, dict) and isinstance(i.get("q"), str) and i["q"].strip()]
        except (OSError, ValueError):
            pass
    return items


async def evaluate(ask: Callable[[str], Awaitable[str]], questions: list[dict[str, Any]] | None = None) -> list[Score]:
    scores = []
    for item in questions if questions is not None else load_questions():
        try:
            answer = await ask(item["q"])
        except Exception as exc:  # a failing model is a failed answer, not a crashed benchmark
            scores.append(Score(item["q"], False, f"no answer ({type(exc).__name__})"))
            continue
        ok, reason = judge(item, answer)
        scores.append(Score(item["q"], ok, reason, answer))
    return scores


def summary(scores: list[Score]) -> dict[str, Any]:
    total = len(scores)
    right = sum(1 for s in scores if s.ok)
    return {"questions": total, "right": right, "score": round(100 * right / total, 1) if total else 0.0,
            "wrong": [s.question for s in scores if not s.ok]}


def previous(data_dir: Path, model: str) -> dict[str, Any] | None:
    try:
        rows = [json.loads(x) for x in (folder(data_dir) / HISTORY).read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return None
    mine = [r for r in rows if r.get("model") == model]
    return mine[-1] if mine else None


def save(data_dir: Path, model: str, scores: list[Score]) -> dict[str, Any]:
    result = summary(scores) | {"model": model, "at": datetime.now().isoformat(timespec="seconds")}
    path = folder(data_dir) / HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def compare(before: dict[str, Any] | None, now: dict[str, Any]) -> list[str]:
    """Plain sentences about what changed since the last run of the same model."""
    if not before:
        return ["First run for this model: nothing to compare with yet."]
    lines = [f"Score {before['score']}% -> {now['score']}%."]
    worse = sorted(set(now["wrong"]) - set(before.get("wrong", [])))
    better = sorted(set(before.get("wrong", [])) - set(now["wrong"]))
    if worse:
        lines.append("Now wrong: " + "; ".join(worse[:5]))
    if better:
        lines.append("Now right: " + "; ".join(better[:5]))
    return lines
