"""Search-by-meaning check: can the embedding model find the right note when the question uses DIFFERENT words - in English, in Bangla, and across the two?

    python run.py --search-eval                              the configured EMBEDDING_MODEL
    python run.py --search-eval --model qwen3-embedding:0.6b another installed embedding model, to compare before switching

A small built-in set: short personal-style notes (English and Bangla) and questions that deliberately avoid the note's own words. For every question the
notes are ranked by cosine similarity of the embeddings ONLY (no keyword help - that is the point), and the report gives, per language pair
(en->en, bn->bn, en->bn, bn->en): hit@1, hit@3 and MRR. Nothing is stored; your memories are not read or changed.

Honest limits: 24 notes and 20 questions written by hand - enough to see a clear difference between two models (e.g. an English-only model failing the
Bangla rows), not a benchmark. A few points of difference are noise. Real search also uses exact words, which this check leaves out on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

import numpy as np

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]

NOTES: dict[str, tuple[str, str]] = {   # id -> (language, text)
    "e1": ("en", "My dentist appointment is on the third Friday of every month at the clinic near the lake."),
    "e2": ("en", "The wifi password for the office router was changed after the break-in."),
    "e3": ("en", "I am allergic to peanuts and shellfish, so check ingredients before ordering."),
    "e4": ("en", "The car needs an oil change every ten thousand kilometres."),
    "e5": ("en", "Rent is due on the fifth and goes to the landlord by bank transfer."),
    "e6": ("en", "My sister Nadia moved to Toronto for her master's degree."),
    "e7": ("en", "The laptop fan gets loud when the graphics card is rendering video."),
    "e8": ("en", "I prefer tea without sugar in the evening and coffee in the morning."),
    "e9": ("en", "The project deadline for the client demo was pushed to the end of the quarter."),
    "e10": ("en", "Backups of the family photos are kept on the external drive in the drawer."),
    "e11": ("en", "I run five kilometres on Tuesday and Thursday before work."),
    "e12": ("en", "The flight to Dubai leaves at midnight and lands before sunrise."),
    "b1": ("bn", "আমার মায়ের জন্মদিন অক্টোবর মাসের বারো তারিখে।"),
    "b2": ("bn", "বাসার বিদ্যুৎ বিল প্রতি মাসের দশ তারিখের মধ্যে দিতে হয়।"),
    "b3": ("bn", "আমি সকালে হাঁটতে যাই এবং তারপর নাস্তা করি।"),
    "b4": ("bn", "অফিসের মিটিং প্রতি সোমবার সকাল দশটায় হয়।"),
    "b5": ("bn", "আমার ছোট ভাই চট্টগ্রামে একটি ব্যাংকে চাকরি করে।"),
    "b6": ("bn", "গাড়ির চাবি সবসময় দরজার পাশের ঝুড়িতে রাখি।"),
    "b7": ("bn", "ডাক্তার বলেছেন প্রতিদিন রাতে খাবারের পরে ওষুধ খেতে।"),
    "b8": ("bn", "আমার প্রিয় খাবার ইলিশ মাছ আর ভাত।"),
    "b9": ("bn", "বাচ্চাদের স্কুল গ্রীষ্মের ছুটিতে দুই সপ্তাহ বন্ধ থাকবে।"),
    "b10": ("bn", "নতুন ফোনটা গত ঈদে কিনেছিলাম।"),
    "b11": ("bn", "বৃষ্টির দিনে ছাদের দরজা দিয়ে পানি ঢোকে, মিস্ত্রি ডাকতে হবে।"),
    "b12": ("bn", "আমি বই পড়তে ভালোবাসি, বিশেষ করে ইতিহাসের বই।"),
}
QUESTIONS: tuple[tuple[str, str, str], ...] = (   # (language, question, expected note id) - worded to avoid the note's own words
    ("en", "When do I get my teeth checked?", "e1"),
    ("en", "Which foods make me sick?", "e3"),
    ("en", "How often should the vehicle be serviced?", "e4"),
    ("en", "Where did my sibling go to study?", "e6"),
    ("en", "Why is my computer noisy?", "e7"),
    ("en", "Where are the pictures saved?", "e10"),
    ("en", "What days do I go jogging?", "e11"),
    ("bn", "আম্মুর জন্ম কবে?", "b1"),
    ("bn", "কারেন্টের টাকা কবে জমা দিতে হয়?", "b2"),
    ("bn", "আমার ভাই কোথায় কাজ করে?", "b5"),
    ("bn", "ঔষধ কখন খেতে হবে?", "b7"),
    ("bn", "আমি কী খেতে সবচেয়ে পছন্দ করি?", "b8"),
    ("bn", "মোবাইল কবে কেনা হয়েছিল?", "b10"),
    ("bn", "ছাদ থেকে পানি পড়ার সমস্যাটা কী?", "b11"),
    ("en", "When is my mother's birthday?", "b1"),
    ("en", "Where do I keep the car keys?", "b6"),
    ("en", "When is the weekly office meeting?", "b4"),
    ("bn", "আমার কোন খাবারে অ্যালার্জি আছে?", "e3"),
    ("bn", "বাড়ি ভাড়া কবে দিতে হয়?", "e5"),
    ("bn", "দুবাইয়ের বিমান কখন ছাড়ে?", "e12"),
)


@dataclass
class Row:
    pair: str
    question: str
    expected: str
    rank: int                      # 1-based position of the right note; 0 = not ranked (should not happen)
    top: str


@dataclass
class Result:
    model: str
    rows: list[Row] = field(default_factory=list)

    def score(self, pair: str | None = None) -> tuple[int, float, float, float]:
        rows = [r for r in self.rows if pair is None or r.pair == pair]
        if not rows:
            return 0, 0.0, 0.0, 0.0
        n = len(rows)
        return n, sum(r.rank == 1 for r in rows) / n, sum(0 < r.rank <= 3 for r in rows) / n, sum(1 / r.rank for r in rows if r.rank) / n


def _unit(vectors: list[list[float]]) -> np.ndarray:
    m = np.asarray(vectors, dtype=np.float32)
    return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)


async def run(embed: Embed, model: str) -> Result:
    from app.memory.semantic import query_text

    ids = list(NOTES)
    notes = _unit(await embed([NOTES[i][1] for i in ids]))
    queries = _unit(await embed([query_text(model, q) for _, q, _ in QUESTIONS]))
    result = Result(model)
    for (lang, question, expected), vec in zip(QUESTIONS, queries):
        order = [ids[i] for i in np.argsort(-(notes @ vec))]
        result.rows.append(Row(f"{lang}->{NOTES[expected][0]}", question, expected, order.index(expected) + 1, order[0]))
    return result


def report(r: Result, previous: "Result | None" = None) -> str:
    lines = [f"Search by meaning - {r.model} ({len(NOTES)} notes, {len(QUESTIONS)} questions, embeddings only):"]
    for pair in ("en->en", "bn->bn", "en->bn", "bn->en"):
        n, h1, h3, mrr = r.score(pair)
        lines.append(f"  {pair}: right note first {h1:.0%}, in the top 3 {h3:.0%}, MRR {mrr:.2f}  ({n} questions)")
    n, h1, h3, mrr = r.score()
    lines.append(f"  ALL:    right note first {h1:.0%}, in the top 3 {h3:.0%}, MRR {mrr:.2f}")
    misses = [x for x in r.rows if x.rank > 3]
    lines += [f"  missed ({x.pair}): \"{x.question}\" - right note ranked {x.rank}" for x in misses[:6]]
    if previous is not None:
        d = r.score()[3] - previous.score()[3]
        lines.append(f"  compared with {previous.model}: MRR {d:+.2f} " + ("(clearly better)" if d >= 0.10 else "(clearly worse)" if d <= -0.10 else "(too close to call)"))
    lines.append("A small hand-written set: a clear gap between two models means something, a few points do not.")
    return "\n".join(lines)
