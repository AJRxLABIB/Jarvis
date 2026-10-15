"""Bangla answer quality: a fixed set of Bangla questions, scored the same way every time.

    python run.py --eval-bangla                  the current model (AI_MODEL)
    python run.py --eval-bangla --model qwen3:8b another installed model, to compare

Each answer is checked with simple, repeatable rules - written in Bangla script, contains the
right fact, short enough - never by another AI. Results go to data\\eval\\bangla.jsonl so models
and versions can be compared over time.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

BENGALI = re.compile(r"[ঀ-৿]")
LETTER = re.compile(r"[A-Za-zঀ-৿]")


@dataclass(frozen=True)
class Question:
    text: str
    contains_any: tuple[str, ...] = ()  # at least one must appear (case-insensitive)
    max_words: int = 80
    bangla: bool = True  # the answer must be mostly Bangla script
    tags: tuple[str, ...] = field(default=())


QUESTIONS: tuple[Question, ...] = (
    Question("বাংলাদেশের রাজধানী কোথায়?", ("ঢাকা",), 40, tags=("fact",)),
    Question("২ আর ৩ যোগ করলে কত হয়?", ("৫", "5", "পাঁচ"), 30, tags=("math",)),
    Question("১০ কে ২ দিয়ে ভাগ করলে কত হয়?", ("৫", "5", "পাঁচ"), 30, tags=("math",)),
    Question("এক বছরে কয় মাস?", ("১২", "12", "বারো"), 30, tags=("fact",)),
    Question("সপ্তাহে কয় দিন?", ("৭", "7", "সাত"), 30, tags=("fact",)),
    Question("পানি কত ডিগ্রি সেলসিয়াসে ফোটে?", ("১০০", "100", "একশ"), 40, tags=("fact",)),
    Question("সূর্য কোন দিকে ওঠে?", ("পূর্ব",), 30, tags=("fact",)),
    Question("বাংলাদেশের জাতীয় ফুল কী?", ("শাপলা",), 30, tags=("fact",)),
    Question("তুমি কে? এক বাক্যে বলো।", ("জার্ভিস", "jarvis", "সহকারী"), 30, tags=("identity",)),
    Question("ইংরেজিতে 'ধন্যবাদ' কে কী বলে?", ("thank",), 30, tags=("translate",)),
    Question("Tell me in Bangla: what is the capital of India?", ("দিল্লি", "দিল্লী"), 40, tags=("mixed",)),
    Question("আমার খুব ক্লান্ত লাগছে, কী করা উচিত?", ("বিশ্রাম", "ঘুম", "পানি", "বিরতি"), 90, tags=("care",)),
    Question("বন্ধুর জন্মদিনে দুই লাইনের একটা শুভেচ্ছা লিখে দাও।", ("জন্মদিন", "শুভ"), 60, tags=("writing",)),
    Question("কম্পিউটারের সিপিইউ কী কাজ করে? সংক্ষেপে বলো।", ("প্রসেস", "হিসাব", "নির্দেশ", "কাজ"), 70, tags=("explain",)),
    Question("একটা ছোট মজার কৌতুক বলো।", (), 90, tags=("chat",)),
)


@dataclass(frozen=True)
class Score:
    question: str
    answer: str
    passed: int
    checks: int
    problems: tuple[str, ...]
    ms: int


def bangla_ratio(text: str) -> float:
    letters = LETTER.findall(text)
    return sum(bool(BENGALI.match(c)) for c in letters) / len(letters) if letters else 0.0


def score(question: Question, answer: str, ms: int = 0) -> Score:
    problems = []
    checks = 1  # something was answered
    answer = (answer or "").strip()
    if not answer:
        problems.append("no answer")
    if question.bangla:
        checks += 1
        if bangla_ratio(answer) < 0.6:
            problems.append("not in Bangla")
    if question.contains_any:
        checks += 1
        lowered = answer.lower()
        if not any(word.lower() in lowered for word in question.contains_any):
            problems.append("wrong or missing fact")
    checks += 1
    if len(answer.split()) > question.max_words:
        problems.append(f"too long ({len(answer.split())} words)")
    return Score(question.text, answer, checks - len(problems), checks, tuple(problems), ms)


async def evaluate(ask: Callable[[str], Awaitable[str]], out: Callable[[str], None] = print,
                   questions: tuple[Question, ...] = QUESTIONS) -> list[Score]:
    """``ask(question) -> answer`` from the model under test."""
    scores = []
    for question in questions:
        started = time.perf_counter()
        try:
            answer = await ask(question.text)
        except Exception as exc:  # reported as a failed answer
            answer = ""
            out(f"  (error: {exc})")
        result = score(question, answer, int((time.perf_counter() - started) * 1000))
        scores.append(result)
        mark = "OK  " if not result.problems else "MISS"
        out(f"{mark} {question.text[:40]:40}  {result.ms:6d} ms  {', '.join(result.problems) or ''}")
        short = " ".join(result.answer.split())[:110]
        out(f"       -> {short}")
    return scores


def summary(scores: list[Score]) -> dict:
    total = sum(s.checks for s in scores)
    passed = sum(s.passed for s in scores)
    return {"questions": len(scores), "fully_right": sum(not s.problems for s in scores),
            "score": round(100 * passed / total, 1) if total else 0.0,
            "avg_ms": int(sum(s.ms for s in scores) / len(scores)) if scores else 0}


def save(data_dir: Path, model: str, scores: list[Score]) -> dict:
    from app import __version__

    result = summary(scores)
    folder = Path(data_dir) / "eval"
    folder.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now().isoformat(timespec="seconds"), "version": __version__, "model": model, **result,
             "misses": [s.question for s in scores if s.problems]}
    with open(folder / "bangla.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return result


def previous(data_dir: Path) -> list[dict]:
    path = Path(data_dir) / "eval" / "bangla.jsonl"
    if not path.is_file():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            runs.append(json.loads(line))
        except ValueError:
            pass
    return runs
