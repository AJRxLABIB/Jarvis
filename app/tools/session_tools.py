"""Interpreter mode and study mode (app/core/sessions.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core import sessions
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class InterpreterArgs(ToolArgs):
    action: Literal["start", "stop"] = Field(default="start", description="start or stop interpreting")
    language: str | None = Field(default=None, max_length=30,
                                 description="Translate everything into this language; empty = Bangla <-> English")


def interpreter_mode(args: InterpreterArgs, ctx: ToolContext) -> ToolResult:
    if args.action == "stop":
        return ToolResult.success(sessions.STATE.stop())
    target = sessions.language_code(args.language)
    if args.language and target is None:
        raise ToolError(f"I can't interpret into {args.language} yet.", "unknown_language")
    if target == "bn" and not args.language:
        target = None
    sessions.STATE.active = sessions.Interpreter(target)
    return ToolResult.success(f"Interpreter on, {sessions.STATE.active.describe()}. Everything you say now is "
                              "translated, nothing is done. Say 'stop interpreting' to finish.")


class StudyArgs(ToolArgs):
    action: Literal["start", "stop"] = Field(default="start", description="start or stop the quiz")
    topic: str | None = Field(default=None, max_length=150, description="What to be quizzed on")
    count: int = Field(default=5, ge=1, le=15, description="How many questions")


async def study_mode(args: StudyArgs, ctx: ToolContext) -> ToolResult:
    if args.action == "stop":
        return ToolResult.success(sessions.STATE.stop())
    if not args.topic:
        raise ToolError("What should I quiz you on?", "missing_topic")
    if ctx.ai is None:
        raise ToolError("The AI model isn't available to write questions.", "no_model")
    material = ""
    if ctx.database is not None and any(w in args.topic.lower() for w in ("my notes", "my document", "my pdf",
                                                                          "my files", "my thesis")):
        try:  # questions from your own indexed documents
            from app.tools.knowledge import document_passages

            material = await document_passages(ctx, args.topic)
        except Exception:
            material = ""
    questions = await sessions.make_quiz(ctx.ai, args.topic, args.count, material)
    if not questions:
        return ToolResult.failure("no_questions", "I couldn't write good questions for that. Try a clearer topic.")
    sessions.STATE.active = sessions.Study(args.topic, questions)
    return ToolResult.success(f"Quiz on {args.topic}: {len(questions)} questions. Say 'stop the quiz' any time. "
                              f"Question 1: {questions[0][0]}", questions=len(questions))


TOOLS: tuple[Tool, ...] = (
    Tool("interpreter_mode", "Interpreter: translate everything the user says (Bangla <-> English, or into a named "
         "language) until they say stop.", interpreter_mode, InterpreterArgs, RiskLevel.SAFE, category="voice"),
    Tool("study_mode", "Study / quiz mode: ask the user questions on a topic, check the answers, keep score.",
         study_mode, StudyArgs, RiskLevel.SAFE, category="voice", timeout=90),
)
