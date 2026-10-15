"""'What am I working on?' - the Context Engine's snapshot (app/core/context_engine.py), with how Jarvis knows."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core import context_engine
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolResult


class ContextArgs(ToolArgs):
    detail: Literal["summary", "full"] = Field(default="summary",
                                              description="summary = what you're doing; full = every source too")


def current_context(args: ContextArgs, ctx: ToolContext) -> ToolResult:
    snap = context_engine.ENGINE.snapshot(ctx)
    text = context_engine.describe(snap)
    data = snap.to_dict()
    if args.detail == "summary":
        data.pop("sources", None)
    return ToolResult.success(text, **data)


class KnowledgeArgs(ToolArgs):
    question: str = Field(min_length=3, max_length=300, description="The question, in the user's words")


def ask_knowledge(args: KnowledgeArgs, ctx: ToolContext) -> ToolResult:
    from app.core import knowledge_graph

    answer, evidence, kind = knowledge_graph.ask(ctx, args.question)
    return ToolResult.success(answer, evidence=evidence, question_type=kind)


async def model_status(_: NoArgs, ctx: ToolContext) -> ToolResult:
    """Which local models are installed, which one does what, and what the last request used."""
    from app.ai.router import is_local_host

    settings = ctx.settings
    installed: list[str] = []
    if ctx.ai is not None:
        try:
            installed = [m.name for m in await ctx.ai.models()]
        except Exception:
            installed = []
    roles = {"main": settings.ai_model, "fast": settings.ai_fast_model, "smart": settings.ai_smart_model,
             "coding": getattr(settings, "ai_coding_model", ""), "vision": settings.vision_model}
    names = {n.lower() for n in installed}
    parts = []
    for role, name in roles.items():
        if not name:
            parts.append(f"{role}: not set" + (" (uses the main model)" if role != "main" else " (the first installed)"))
        else:
            ok = name.lower() in names or f"{name.lower()}:latest" in names
            parts.append(f"{role}: {name}" + ("" if ok or not installed else " (NOT installed - I use the main model)"))
    last = getattr(ctx.ai, "last_route", None) if ctx.ai is not None else None
    where = "on this PC or your network" if is_local_host(settings.ollama_host) else \
        f"on {settings.ollama_host} (the internet: private requests are refused)"
    message = (f"Models run {where}. " + "; ".join(parts) + ". Installed: " + (", ".join(installed) or "unknown "
               "(Ollama isn't answering)") + ".")
    if last:
        message += f" Last request: {last['model']} ({last['role']})" + (
            f", after {last['fallback']}" if last.get("fallback") else "") + "."
    health = ctx.ai.health.summary() if ctx.ai is not None and hasattr(ctx.ai, "health") else {}
    skipped = [f"{name} (failing; skipped for {info['skipped_for']} s)" for name, info in health.items()
               if info["skipped_for"] > 0]
    if skipped:
        message += " Temporarily skipped because of repeated failures: " + ", ".join(skipped) + "."
    return ToolResult.success(message, installed=installed, roles=roles, last=last, health=health)


class ProactiveArgs(ToolArgs):
    action: Literal["status", "why", "set_level", "more", "less", "quiet_hours"] = Field(
        description="status = settings and recent suggestions; why = why the last suggestion was made; set_level; "
                    "more / less = one level up or down; quiet_hours = set them")
    level: Literal["off", "minimal", "normal", "active", "high"] | None = Field(default=None,
                                                                                description="For set_level")
    hours: str | None = Field(default=None, max_length=20, description="For quiet_hours: '22:00-07:00', or '' for none")


def proactive_control(args: ProactiveArgs, ctx: ToolContext) -> ToolResult:
    from app.core import proactive
    from app.tools.base import ToolError

    order = list(proactive.LEVELS)
    current = getattr(ctx.settings, "proactive_level", "normal")
    if args.action in ("status", "why"):
        recent = [d for d in proactive.ENGINE.recent(20) if d.outcome != "drop"] if args.action == "why" else \
            proactive.ENGINE.recent(5)
        if args.action == "why":
            if not recent:
                return ToolResult.success("I haven't suggested anything recently.")
            return ToolResult.success(proactive.explain(recent[0]))
        info = proactive.status(ctx.settings)
        lines = [proactive.explain(d) for d in recent]
        return ToolResult.success(f"Proactive level: {info['level']}; quiet hours: {info['quiet_hours'] or 'none'}; at "
                                  f"most {info['max_per_hour']} spoken per hour; kinds: {info['categories']}."
                                  + (" Recently: " + " | ".join(lines) if lines else ""), **info)
    if ctx.apply_setting is None:
        raise ToolError("Settings can only be changed while the Jarvis window is open.", "unavailable")
    if args.action == "quiet_hours":
        hours = (args.hours or "").strip()
        if hours and proactive.parse_quiet_hours(hours) is None:
            raise ToolError("Say quiet hours like 22:00-07:00.", "invalid")
        ctx.apply_setting("quiet_hours", hours)
        return ToolResult.success(f"Quiet hours: {hours or 'none'}.")
    if args.action == "set_level":
        if args.level is None:
            raise ToolError("Which level: off, minimal, normal, active or high?", "missing_level")
        level = args.level
    else:
        index = order.index(current) if current in order else 2
        level = order[min(len(order) - 1, index + 1) if args.action == "more" else max(0, index - 1)]
    ctx.apply_setting("proactive_level", level)
    return ToolResult.success(f"Proactive level: {level}.", level=level, previous=current)


TOOLS: tuple[Tool, ...] = (
    Tool("current_context", "What the user is doing right now: the app and window they're in, the project, file, "
         "git branch and uncommitted changes, goal, mission, approval waiting, PC state - and how sure Jarvis is. "
         "For 'what am I working on', 'what do you see me doing', 'what's my current context'.", current_context,
         ContextArgs, RiskLevel.SAFE, category="memory", timeout=20),
    Tool("ask_knowledge", "Answer from the user's own knowledge graph (projects, files, decisions, memories, goals, "
         "missions, requests), with sources: 'which files belong to TMS', 'what decision did I make about the "
         "database', 'what was I working on last week', 'which project uses Laravel', 'everything related to X'.",
         ask_knowledge, KnowledgeArgs, RiskLevel.SAFE, category="memory", timeout=60),
    Tool("model_status", "Which AI models Jarvis has and uses: main, fast, smart, coding, vision, installed ones, and "
         "which model answered the last request (and any fallback). 'which model are you using'.", model_status,
         NoArgs, RiskLevel.SAFE, category="basic", timeout=20),
    Tool("proactive_control", "How proactive Jarvis is: status, why it suggested something, set the level (off / "
         "minimal / normal / active / high), one step more or less, quiet hours.", proactive_control, ProactiveArgs,
         RiskLevel.SAFE, category="voice"),
)
