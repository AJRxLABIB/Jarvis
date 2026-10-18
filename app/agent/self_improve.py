"""Self-improvement with proof: Jarvis tries better set-ups for its own brain and keeps one ONLY when a test built from
YOUR real requests says it is better. Nothing is changed on a hunch, and nothing unsafe is ever "improved away".

What it can change (the levers that really make a local model smarter at YOUR tasks):
* which installed model "jarvis" is built from (any installed tool-capable model that fits this PC's memory);
* how creative it is (AI_TEMPERATURE: lower = more exact tool use).
What it measures, on the personal test (app/agent/growth.py cases(): requests whose right tool is known from real use):
* does it choose the right tool; does it make malformed / unknown tool calls; how long it takes.
The rule: a candidate replaces the current set-up only if it gets at least ``MIN_GAIN`` more right (or the same
score and ``FASTER`` quicker), with at least ``MIN_CASES`` test requests - otherwise everything stays as it is and the
report says why. Every run is written to data/self_improve.jsonl.

What it never does: change its own permissions, safety rules or code (code changes go through the Evolution lab:
tested copy + your approval), send anything off this PC, or install models by itself.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

MIN_CASES = 8
MIN_GAIN = 0.05
FASTER = 0.8                  # 20 % quicker counts when the score is equal
MAX_CANDIDATES = 4
HISTORY = "self_improve.jsonl"


@dataclass
class Candidate:
    base: str
    temperature: float
    label: str = ""


@dataclass
class Score:
    candidate: Candidate
    cases: int = 0
    right: int = 0
    malformed: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.right / self.cases if self.cases else 0.0

    @property
    def avg_seconds(self) -> float:
        return self.seconds / self.cases if self.cases else 0.0


# ask(model, temperature, request_text) -> (tool name chosen or None, malformed?)
Ask = Callable[[str, float, str], Awaitable[tuple[str | None, bool]]]


async def evaluate(ask: Ask, model: str, candidate: Candidate, cases: list[dict], clock=time.monotonic) -> Score:
    score = Score(candidate)
    for case in cases:
        started = clock()
        try:
            chosen, malformed = await ask(model, candidate.temperature, case["request"])
        except Exception as exc:                         # a failing model scores zero for that case, honestly
            score.errors.append(f"{exc.__class__.__name__}: {str(exc)[:80]}")
            chosen, malformed = None, False
        score.seconds += clock() - started
        score.cases += 1
        score.right += int(chosen == case["expect_tool"])
        score.malformed += int(malformed)
    return score


def better(new: Score, current: Score) -> tuple[bool, str]:
    if new.cases < MIN_CASES:
        return False, f"only {new.cases} test request(s); I need at least {MIN_CASES} from your real use"
    if new.malformed > current.malformed:
        return False, "it made more broken tool calls"
    if new.accuracy >= current.accuracy + MIN_GAIN:
        return True, f"right tool {new.accuracy:.0%} vs {current.accuracy:.0%}"
    if new.accuracy >= current.accuracy and current.avg_seconds >= 0.05 and new.avg_seconds <= current.avg_seconds * FASTER:
        return True, f"same score, {current.avg_seconds / max(new.avg_seconds, 0.01):.1f}x faster"
    return False, f"not better (right tool {new.accuracy:.0%} vs {current.accuracy:.0%})"


def candidates(current_base: str, current_temperature: float, models, total_ram: int | None) -> list[Candidate]:
    """The current set-up first, then lower temperature, then other installed tool-capable models that fit."""
    from app.ai.health import could_ever_fit
    from app.ai.jarvis_model import is_jarvis

    out = [Candidate(current_base, current_temperature, "current")]
    if current_temperature > 0.15:
        out.append(Candidate(current_base, round(max(0.1, current_temperature - 0.15), 2), "more exact"))
    others = [m for m in models if m.can_chat and m.supports_tools and not m.supports_vision and not is_jarvis(m.name)
              and m.name.split(":")[0] != current_base.split(":")[0] and could_ever_fit(m.size_bytes, total_ram)]
    for m in sorted(others, key=lambda m: -(m.size_bytes or 0)):
        out.append(Candidate(m.name, current_temperature, "other model"))
    return out[:MAX_CANDIDATES]


@dataclass
class Outcome:
    changed: bool
    message: str
    scores: list[dict] = field(default_factory=list)
    winner: dict | None = None


async def run(ask: Ask, apply: Callable[[Candidate], Awaitable[None]], cases: list[dict], options: list[Candidate],
              data_dir: Path | None = None, *, model_for: Callable[[Candidate], str] | None = None) -> Outcome:
    """Score every option on the same personal test, keep the best only if it beats the current one."""
    if len(cases) < MIN_CASES:
        return Outcome(False, f"Not enough to test on yet: I have {len(cases)} of your requests with a known right "
                              f"answer and need {MIN_CASES}. Keep using me normally - every request teaches me.")
    scores = []
    for option in options:
        model = model_for(option) if model_for else option.base
        scores.append(await evaluate(ask, model, option, cases))
    current = scores[0]
    best = max(scores[1:], key=lambda s: (s.accuracy, -s.malformed, -s.avg_seconds), default=None)
    rows = [{"base": s.candidate.base, "temperature": s.candidate.temperature, "label": s.candidate.label,
             "accuracy": round(s.accuracy, 3), "malformed": s.malformed, "avg_seconds": round(s.avg_seconds, 2),
             "errors": s.errors[:3]} for s in scores]
    if best is None:
        outcome = Outcome(False, "There is nothing else to try (one model installed, already exact).", rows)
    else:
        ok, why = better(best, current)
        if ok:
            await apply(best.candidate)
            outcome = Outcome(True, f"Improved: now using {best.candidate.base} at temperature "
                                    f"{best.candidate.temperature} ({why}).", rows, asdict(best.candidate))
        else:
            outcome = Outcome(False, f"Kept the current set-up ({current.accuracy:.0%} right on {current.cases} of "
                                     f"your requests): the best alternative, {best.candidate.base} at "
                                     f"{best.candidate.temperature}, was {why}.", rows)
    if data_dir is not None:
        try:
            path = Path(data_dir) / HISTORY
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                         "changed": outcome.changed, "message": outcome.message, "scores": rows}) + "\n")
        except OSError:
            pass
    return outcome


def history(data_dir: Path, limit: int = 5) -> list[dict[str, Any]]:
    try:
        lines = (Path(data_dir) / HISTORY).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def export_training(database, path: Path, limit: int = 2000) -> int:
    """Your good requests (and the answers you liked) as chat-format JSONL - for an OPTIONAL fine-tune with an
    external tool (e.g. a LoRA trainer on a GPU). Redacted; private-mode requests were never recorded."""
    rows = []
    with database.connect() as conn:
        for r in conn.execute("SELECT request, tools FROM learning_turns WHERE outcome = 'good' ORDER BY id DESC "
                              "LIMIT ?", (limit,)):
            tools = [t for t in json.loads(r["tools"] or "[]") if t["ok"]]
            if tools:
                calls = [{"type": "function", "function": {"name": t["name"], "arguments": t.get("args") or {}}}
                         for t in tools]
                rows.append({"messages": [{"role": "user", "content": r["request"]},
                                          {"role": "assistant", "content": "", "tool_calls": calls}]})
        for r in conn.execute("SELECT question, answer FROM answer_examples ORDER BY id DESC LIMIT ?", (limit,)):
            rows.append({"messages": [{"role": "user", "content": r["question"]},
                                      {"role": "assistant", "content": r["answer"]}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)
