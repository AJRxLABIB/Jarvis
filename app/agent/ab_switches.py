"""Measure an optional switch on YOUR real requests before turning it on:  python run.py --ab-test

Compares the normal tool choice with the two-step tool choice (``TWO_STEP_TOOLS``, app/agent/tool_picker.py) on the personal test set - requests from
your own use whose right tool is known (app/agent/growth.py ``cases``). Same model, same temperature, same requests; only the switch differs.
Reported per side: how often the right tool was chosen (a request you once had to correct counts double), broken tool calls, seconds per request.
The verdict uses the same rule as self-improvement (``self_improve.better``): the switch is recommended only for a clear gain with no more broken
calls, and never from fewer than ``MIN_CASES`` requests. Nothing is changed: the command prints the result and you edit ``.env`` yourself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.agent import self_improve as SI


async def compare(ask_plain: SI.Ask, ask_switch: SI.Ask, model: str, temperature: float, cases: list[dict], name: str = "two-step tool choice") -> str:
    if len(cases) < SI.MIN_CASES:
        return (f"Not enough history to measure: {len(cases)} request(s) with a known right tool, {SI.MIN_CASES} needed. "
                "Use Jarvis normally for a while (and tell it when it picked wrong), then run this again.")
    base = await SI.evaluate(ask_plain, model, SI.Candidate(model, temperature, "normal"), cases)
    new = await SI.evaluate(ask_switch, model, SI.Candidate(model, temperature, name), cases)
    ok, why = SI.better(new, base)

    def line(s: SI.Score) -> str:
        return f"  {s.candidate.label:22} right tool {s.accuracy:.0%} ({s.right} of {s.cases}), broken calls {s.malformed}, {s.avg_seconds:.1f} s per request" \
               + (f", {len(s.errors)} error(s)" if s.errors else "")

    verdict = (f"RECOMMENDED: {name} is better here ({why}). To use it set TWO_STEP_TOOLS=true in .env." if ok
               else f"KEEP IT OFF: {why}.")
    return "\n".join([f"A/B on {len(cases)} of your own requests, model {model}:", line(base), line(new), verdict,
                      "One run on a small set can swing by a few requests; repeat it before deciding on a close result."])


async def run_real(settings: Any, database: Any, registry: Any) -> str:
    """The real comparison against Ollama. The decision logic above is tested with fakes; this wiring is unverified until you run it."""
    from app.agent.growth import Growth
    from app.agent.improve_job import _ask_factory
    from app.agent.tool_picker import narrow
    from app.agent.tool_router import select_specs
    from app.ai import jarvis_model
    from app.ai.base import ChatMessage, ChatRequest
    from app.ai.manager import create_provider
    from app.ai.prompts import build_system_prompt

    cases = Growth(database).cases()
    provider = create_provider(settings)
    try:
        model = settings.ai_model
        if not model:
            chosen = jarvis_model.choose_base(await provider.list_models())
            model = chosen.name if chosen else ""
        if not model:
            return "No chat model is installed."
        temperature = float(settings.ai_temperature)
        ask_plain, providers = _ask_factory(settings, registry)
        basics = [t.name for t in registry.tools if t.category == "basic"]

        class Picker:                                   # the minimal "structured" surface tool_picker needs, on the same provider
            async def structured(self, messages, schema, fast=True):
                import json

                r = await provider.chat(ChatRequest(model=model, messages=tuple(messages), format=schema, think=None))
                data = json.loads(r.message.content or "")
                return data if isinstance(data, dict) else None

        async def ask_switch(m: str, temp: float, text: str):
            specs = await narrow(Picker(), text, select_specs(registry, text), keep=basics)
            system = build_system_prompt(assistant_name=settings.assistant_name, now=datetime.now(), tools=specs, user_address=settings.user_address)
            response = await provider.chat(ChatRequest(model=m, messages=(ChatMessage.system(system), ChatMessage.user(text)), tools=tuple(specs)))
            calls = response.message.tool_calls
            if not calls:
                return None, False
            tool = registry.get(calls[0].name)
            if tool is None:
                return calls[0].name, True
            try:
                tool.args_model.model_validate(calls[0].arguments or {})
            except Exception:
                return calls[0].name, True
            return calls[0].name, False

        try:
            return await compare(ask_plain, ask_switch, model, temperature, cases)
        finally:
            for p in providers.values():
                close = getattr(p, "aclose", None)
                if close is not None:
                    await close()
    finally:
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()
