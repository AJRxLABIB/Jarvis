"""Long-term memory retrieval check in the LongMemEval format: given many past chat sessions, can Jarvis FIND the session that holds the answer?

    python run.py --longmem-eval                 the 10 built-in cases (no download)
    python run.py --longmem-eval path\\to\\longmemeval_s.json     the public dataset file, if you downloaded it
    python run.py --longmem-eval ... --full      also ask the chat model to answer from what was retrieved

What is measured, and what is not:
* RETRIEVAL - for each question, every session in its "haystack" is scored with Jarvis's own keyword scorer (``app.memory.semantic``: the same
  ``query_terms`` / ``keyword_score`` the real index uses) and ranked; a hit = one of the answer sessions is in the top ``k``. Reported per question type
  as recall@k. No model is needed. Meaning-based (embedding) search is NOT included here, so questions worded differently from the session score lower
  than the real index would with an embedding model - this is a floor, not Jarvis's best.
* ANSWERS (only with --full) - the model answers from the retrieved sessions and the reply is checked by whether it contains the gold answer's key words.
  That is a simple containment check, NOT the benchmark's official LLM judge, so the number is not comparable with published LongMemEval scores.
* "Abstention" questions (the answer is in no session) have no retrieval target; with --full a correct reply says it does not know.
The built-in cases are tiny and hand-written; they show the harness works, not how good the memory is. Nothing here reads or changes your own memories.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

K = 5
SYSTEM = ("Answer the question using ONLY these past chat sessions. You may combine facts from several sessions, and when the same fact changed over time the LATEST session is correct. "
          "Answer in one short sentence. Only if no session states the answer (a session about a different thing, e.g. a dog when asked about a cat, is not an answer) "
          "reply: \"I don't know - that is not in the saved sessions.\" Never guess.")
ABSTAIN = ("don't know", "do not know", "not sure", "no record", "not mentioned", "cannot find", "can't find", "no information", "didn't mention", "did not mention")


@dataclass
class Case:
    id: str
    kind: str
    question: str
    answer: str
    sessions: dict[str, str]              # session id -> its text (user and assistant turns)
    answer_sessions: list[str]

    @property
    def abstention(self) -> bool:
        return self.id.endswith("_abs") or not self.answer_sessions


@dataclass
class Score:
    cases: int = 0
    retrieval: dict[str, list[int]] = field(default_factory=dict)      # kind -> [hits, total]
    answers: dict[str, list[int]] = field(default_factory=dict)
    skipped: int = 0


def _text(session: Any) -> str:
    if isinstance(session, str):
        return session
    return "\n".join(str(t.get("content", "")) for t in session if isinstance(t, dict))


def load(path: Path) -> list[Case]:
    """The public LongMemEval JSON layout: a list of objects with question_id, question_type, question, answer, haystack_session_ids,
    haystack_sessions and answer_session_ids. Entries that do not have that shape are skipped (counted by the caller)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for r in raw if isinstance(raw, list) else []:
        try:
            ids, sessions = r["haystack_session_ids"], r["haystack_sessions"]
            out.append(Case(str(r["question_id"]), str(r.get("question_type", "unknown")), str(r["question"]), str(r.get("answer", "")),
                            {str(i): _text(s) for i, s in zip(ids, sessions)}, [str(x) for x in r.get("answer_session_ids", [])]))
        except (KeyError, TypeError):
            continue
    return out


def rank(question: str, sessions: dict[str, str], k: int = K) -> list[str]:
    from app.memory.semantic import keyword_score, query_terms

    terms = query_terms(question)
    scored = sorted(((keyword_score(terms, text), sid) for sid, text in sessions.items()), key=lambda x: (-x[0], x[1]))
    return [sid for s, sid in scored[:k] if s > 0]


def _gold_words(answer: str) -> list[str]:
    from app.memory.semantic import query_terms

    return query_terms(answer)[:6]


def answer_ok(case: Case, reply: str) -> bool:
    low = (reply or "").lower()
    if case.abstention:
        return any(p in low for p in ABSTAIN)
    words = _gold_words(case.answer)
    if not words:
        return False
    found = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", low))
    return found / len(words) >= 0.6


def run(cases: list[Case], ask: Callable[[str, str], str] | None = None, k: int = K) -> Score:
    sc = Score()
    for c in cases:
        sc.cases += 1
        top = rank(c.question, c.sessions, k)
        if not c.abstention:
            hit = sc.retrieval.setdefault(c.kind, [0, 0])
            hit[1] += 1
            hit[0] += int(any(s in top for s in c.answer_sessions))
        if ask is not None:
            context = "\n\n".join(f"[session {sid}]\n{c.sessions[sid][:1500]}" for sid in top)
            try:
                reply = ask(c.question, context)
            except Exception:
                sc.skipped += 1
                continue
            a = sc.answers.setdefault(c.kind, [0, 0])
            a[1] += 1
            a[0] += int(answer_ok(c, reply))
    return sc


def report(sc: Score, source: str, k: int = K, with_answers: bool = False) -> str:
    def line(name: str, pair: list[int]) -> str:
        return f"  {name}: {pair[0]} of {pair[1]} ({pair[0] / pair[1]:.0%})" if pair[1] else f"  {name}: no cases"

    total = [sum(v[0] for v in sc.retrieval.values()), sum(v[1] for v in sc.retrieval.values())]
    lines = [f"Long-term memory retrieval check - {source}, {sc.cases} question(s).", f"Retrieval recall@{k} (keyword scorer only; a floor, embeddings not used):",
             *[line(kind, pair) for kind, pair in sorted(sc.retrieval.items())], line("ALL", total)]
    if with_answers:
        at = [sum(v[0] for v in sc.answers.values()), sum(v[1] for v in sc.answers.values())]
        lines += ["Answers from the chat model (containment check, NOT the official judge - not comparable with published scores):",
                  *[line(kind, pair) for kind, pair in sorted(sc.answers.items())], line("ALL", at)]
        if sc.skipped:
            lines.append(f"  {sc.skipped} question(s) got no answer from the model and are not counted.")
    if "built-in" in source:
        lines.append("The built-in cases are few and hand-written: they show the check runs, not how good the memory is.")
    return "\n".join(lines)


def _s(*turns: str) -> str:
    return "\n".join(turns)


def builtin() -> list[Case]:
    filler = {"f1": _s("Can you recommend a pasta recipe?", "Try aglio e olio with garlic and chilli."), "f2": _s("What is the capital of Australia?", "Canberra."),
              "f3": _s("Help me write a birthday message for my aunt.", "Happy birthday to a wonderful aunt!"), "f4": _s("How do I reset my router?", "Hold the reset button for ten seconds."),
              "f5": _s("Suggest a workout for beginners.", "Start with a 20 minute walk and bodyweight squats.")}

    def case(cid, kind, q, a, own: dict[str, str], gold: list[str]) -> Case:
        return Case(cid, kind, q, a, {**filler, **own}, gold)

    return [
        case("b1", "single-session-user", "What breed is my dog?", "golden retriever", {"s1": _s("I just adopted a golden retriever puppy called Max.", "Congratulations on Max!")}, ["s1"]),
        case("b2", "single-session-user", "Which university did I say I graduated from?", "BUET", {"s1": _s("I graduated from BUET in 2019 with a CSE degree.", "That is a strong programme.")}, ["s1"]),
        case("b3", "single-session-assistant", "Which library did you suggest for charts in my dashboard?", "Plotly",
             {"s1": _s("Which library should I use for charts in my dashboard?", "I suggest Plotly for interactive charts.")}, ["s1"]),
        case("b4", "multi-session", "How many programming languages did I say I use at work?", "two",
             {"s1": _s("At work I mostly write Python.", "Python is a good fit."), "s2": _s("At work I also write some Go for services.", "Go is good for services.")}, ["s1", "s2"]),
        case("b5", "knowledge-update", "Where do I work now?", "Fluxit",
             {"s1": _s("I work at Brightsoft as a developer.", "Noted."), "s2": _s("Update: I left Brightsoft, I now work at Fluxit.", "Congratulations on the new job at Fluxit.")}, ["s2"]),
        case("b6", "knowledge-update", "What phone do I use now?", "Pixel 9",
             {"s1": _s("My phone is a Galaxy S21.", "Good phone."), "s2": _s("I replaced my phone, I use a Pixel 9 now.", "Enjoy the Pixel 9.")}, ["s2"]),
        case("b7", "temporal-reasoning", "Which trip did I mention first, the Sylhet trip or the Cox's Bazar trip?", "Sylhet",
             {"s1": _s("[March] We are planning a trip to Sylhet next month.", "Sylhet is lovely in spring."), "s2": _s("[June] The Cox's Bazar trip is booked for July.", "Enjoy the beach.")}, ["s1", "s2"]),
        case("b8", "single-session-preference", "What kind of coffee do I prefer?", "black, no sugar", {"s1": _s("I always drink my coffee black, no sugar.", "I will remember: black, no sugar.")}, ["s1"]),
        case("b9", "single-session-user", "What is the name of my project's database server?", "orion-db",
             {"s1": _s("Our project database server is called orion-db and runs MySQL 8.", "Noted: orion-db, MySQL 8.")}, ["s1"]),
        case("b10_abs", "abstention", "What is my cat's name?", "", {"s1": _s("I just adopted a golden retriever puppy called Max.", "Congratulations on Max!")}, []),
    ]
