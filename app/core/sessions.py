"""Conversation sessions that change what your next words mean, until you stop them.

* Interpreter: everything you say is translated (Bangla <-> English, or into one language you name) and spoken;
  nothing is treated as a command except "stop interpreting".
* Study: Jarvis quizzes you on a topic - asks, checks your answer, explains, keeps the score.

The model only translates / writes questions / judges an answer here; it gets no tools in a session, so
nothing said during a session can start an action.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.ai.base import ChatMessage

LANGUAGES = {"bangla": "bn", "bengali": "bn", "english": "en", "hindi": "hi", "arabic": "ar", "urdu": "ur",
             "spanish": "es", "french": "fr", "german": "de", "chinese": "zh", "japanese": "ja", "korean": "ko",
             "turkish": "tr", "russian": "ru", "malay": "ms", "indonesian": "id", "italian": "it",
             "portuguese": "pt"}
NAMES = {code: name.capitalize() for name, code in LANGUAGES.items() if name != "bengali"}
_STOP = re.compile(r"^(?:please\s+)?(?:jarvis\s*,?\s+)?(?:stop|end|exit|quit|cancel|finish)\s*(?:the\s+)?"
                   r"(?:interpret(?:er|ing)?(?:\s+mode)?|translat(?:ion|ing|or)(?:\s+mode)?|quiz|study(?:\s+mode)?|"
                   r"session|it|now)?[.!]*$|^(?:বন্ধ\s*করো|থামো)[।.!]*$", re.I)


@dataclass
class Interpreter:
    target: str | None = None  # None = Bangla <-> English, whichever you didn't speak

    def describe(self) -> str:
        return f"into {NAMES.get(self.target, self.target)}" if self.target else "between Bangla and English"


@dataclass
class Study:
    topic: str
    questions: list[tuple[str, str]] = field(default_factory=list)
    index: int = 0
    score: int = 0

    @property
    def current(self) -> tuple[str, str] | None:
        return self.questions[self.index] if self.index < len(self.questions) else None


class SessionState:
    def __init__(self) -> None:
        self.active: Interpreter | Study | None = None

    def stop(self) -> str:
        was, self.active = self.active, None
        if isinstance(was, Study):
            return f"Quiz over: {was.score} of {was.index} right."
        if isinstance(was, Interpreter):
            return "Interpreter off."
        return "Nothing to stop."


STATE = SessionState()


def is_stop(text: str) -> bool:
    return bool(_STOP.match(text.strip()))


def language_code(name: str | None) -> str | None:
    if not name:
        return None
    text = name.strip().lower()
    return LANGUAGES.get(text) or (text if text in NAMES else None)


async def translate(ai, text: str, interpreter: Interpreter, spoken_language: str) -> tuple[str, str]:
    """(translation, its language code)."""
    target = interpreter.target or ("en" if spoken_language == "bn" else "bn")
    name = NAMES.get(target, target)
    style = " Use natural spoken Bangladeshi Bangla in Bengali script." if target == "bn" else ""
    response = await ai.chat([
        ChatMessage.system(f"You are an interpreter. Translate the user's words into {name}.{style} Output ONLY the "
                           "translation - no quotes, no notes, no answers to questions in it."),
        ChatMessage.user(text)], fast=True)
    return (response.message.content or "").strip().strip('"'), target


def parse_questions(text: str, limit: int) -> list[tuple[str, str]]:
    """Questions from the model: a JSON list of {"q","a"}, or lines 'Q: ... A: ...'."""
    found: list[tuple[str, str]] = []
    match = re.search(r"\[.*\]", text, re.S)
    if match:
        try:
            for item in json.loads(match.group(0)):
                if isinstance(item, dict) and str(item.get("q", "")).strip() and str(item.get("a", "")).strip():
                    found.append((str(item["q"]).strip(), str(item["a"]).strip()))
        except ValueError:
            found = []
    if not found:
        for q, a in re.findall(r"Q\d*[:.)]\s*(.+?)\s*(?:\n\s*)?A\d*[:.)]\s*(.+?)(?=\n\s*Q\d*[:.)]|\Z)", text, re.S):
            found.append((" ".join(q.split()), " ".join(a.split())))
    return found[:limit]


async def make_quiz(ai, topic: str, count: int, material: str = "") -> list[tuple[str, str]]:
    source = f"Use only this material:\n{material[:6000]}\n\n" if material else ""
    response = await ai.chat([
        ChatMessage.system("You write short study quizzes. Reply with ONLY a JSON list like "
                           '[{"q": "question", "a": "short correct answer"}].'),
        ChatMessage.user(f"{source}Write {count} short questions (one fact each) about: {topic}")])
    return parse_questions(response.message.content or "", count)


async def check_answer(ai, question: str, expected: str, answer: str) -> tuple[bool, str]:
    response = await ai.chat([
        ChatMessage.system("You grade a student's answer. Reply 'CORRECT' or 'WRONG' first, then one short sentence "
                           "explaining. Accept answers with the same meaning or small spelling mistakes."),
        ChatMessage.user(f"Question: {question}\nCorrect answer: {expected}\nStudent's answer: {answer}")], fast=True)
    text = (response.message.content or "").strip()
    right = text.upper().startswith("CORRECT")
    explanation = re.sub(r"^(?:CORRECT|WRONG)[\s.:!,-]*", "", text, flags=re.I).strip()
    return right, explanation
