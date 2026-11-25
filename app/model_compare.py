"""Which model answers best? The same Bangla question set (app/eval_bangla.py) for each installed
chat model, scored the same way, plus the time per answer.

    python run.py --compare-models                       every installed chat model (up to 4)
    python run.py --compare-models qwen3:4b-instruct,qwen3:8b
    python run.py --compare-models --switch              ... and make "jarvis" from the winner

The winner is the best score; if two are within 3 points, the faster one wins (a snappier Jarvis is
worth more than a hair more accuracy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from app import eval_bangla

CLOSE_POINTS = 3.0
MAX_MODELS = 4


@dataclass(frozen=True)
class ModelScore:
    model: str
    score: float
    fully_right: int
    questions: int
    avg_ms: int


def pick_winner(scores: list[ModelScore]) -> ModelScore | None:
    if not scores:
        return None
    best = max(s.score for s in scores)
    close = [s for s in scores if best - s.score <= CLOSE_POINTS]
    return min(close, key=lambda s: s.avg_ms)


async def compare(models: list[str], ask_for: Callable[[str], Callable[[str], Awaitable[str]]],
                  out: Callable[[str], None] = print, questions=eval_bangla.QUESTIONS,
                  prepare: Callable[[str], Awaitable[object]] | None = None) -> list[ModelScore]:
    """``ask_for(model)`` -> ask(question) -> answer, using that model. ``prepare(model)`` loads it first,
    so loading time doesn't count against the model's speed."""
    results = []
    for model in models[:MAX_MODELS]:
        out(f"\n=== {model} ===")
        if prepare is not None:
            try:
                await prepare(model)
            except Exception:
                pass
        scores = await eval_bangla.evaluate(ask_for(model), out, questions)
        summary = eval_bangla.summary(scores)
        results.append(ModelScore(model, summary["score"], summary["fully_right"], summary["questions"],
                                  summary["avg_ms"]))
    out("\nModel                          score   right   time/answer")
    for s in sorted(results, key=lambda s: -s.score):
        out(f"{s.model[:30]:30} {s.score:5.1f}%  {s.fully_right:2d}/{s.questions:<2d}  {s.avg_ms / 1000:6.1f} s")
    winner = pick_winner(results)
    if winner is not None:
        out(f"\nBest for you: {winner.model}")
    return results


async def compare_installed(settings, names: str | None = None, out: Callable[[str], None] = print,
                            questions=eval_bangla.QUESTIONS) -> list[ModelScore]:
    """Compare the named models (comma list), or every installed chat model, with real Ollama answers."""
    from datetime import datetime

    from app.ai import jarvis_model
    from app.ai.base import ChatMessage
    from app.ai.manager import AIManager
    from app.ai.ollama import strip_think
    from app.ai.prompts import build_system_prompt

    probe = AIManager.from_settings(settings)
    managers: dict[str, AIManager] = {}
    try:
        installed = await probe.refresh_models()
        chat = [m.name for m in installed
                if m.can_chat and not m.is_vision_only and not jarvis_model.is_jarvis(m.name)]
        models = [n.strip() for n in names.split(",") if n.strip()] if names else chat
        if not models:
            out("No chat models to compare: install one with  ollama pull qwen3:4b-instruct")
            return []
        system = build_system_prompt(assistant_name=settings.assistant_name, now=datetime.now(),
                                     reply_language="bn", spoken=True)

        def ask_for(model: str):
            if model not in managers:  # one manager per model (prepare() may have made it already)
                managers[model] = AIManager.from_settings(settings.model_copy(update={"ai_model": model}))

            async def ask(question: str) -> str:
                reply = await managers[model].chat([ChatMessage.system(system), ChatMessage.user(question)])
                return strip_think(reply.message.content or "")
            return ask

        out(f"Comparing {len(models[:MAX_MODELS])} model(s) on {len(questions)} Bangla questions...")

        async def prepare(model: str) -> None:
            ask_for(model)
            await managers[model].warm_up()

        return await compare(models, ask_for, out, questions, prepare)
    finally:
        for manager in [*managers.values(), probe]:
            try:
                await manager.aclose()
            except Exception:
                pass
