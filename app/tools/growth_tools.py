"""``learning`` ("what have you learned?", "how are you improving?", "forget what you learned") and
``tune_ai`` (run the measured self-improvement test in the background and report the result)."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from app.agent.growth import Growth
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult


class LearningArgs(ToolArgs):
    action: Literal["report", "lessons", "forget"] = Field(
        default="report", description="report = what I learned and whether I'm improving; lessons = the latest "
                                      "lessons; forget = erase everything learned from use")


def _db(ctx: ToolContext):
    if ctx.database is None:
        raise ToolError("Learning needs the local database.", "no_database")
    return ctx.database


def learning(args: LearningArgs, ctx: ToolContext) -> ToolResult:
    growth = Growth(_db(ctx))
    if args.action == "forget":
        removed = growth.forget()
        return ToolResult.success(f"Forgot everything I learned from use ({removed} skills and lessons, and the "
                                  "request history). Your memories and settings are unchanged.")
    report = growth.report()
    if args.action == "lessons":
        lessons = report["latest_lessons"]
        return ToolResult.success("Latest lessons: " + " | ".join(lessons) + "." if lessons else
                                  "No lessons yet - they come from requests you corrected.", lessons=lessons)
    if not report["turns"]:
        return ToolResult.success("I haven't finished learning from any requests yet - just use me; every request "
                                  "teaches me what works.", **report)
    trend = report["trend"]
    weekly = ", ".join(f"{t['weeks_ago']}w ago {t['rate']:.0%}" if t["weeks_ago"] else f"this week {t['rate']:.0%}"
                       for t in trend if t["rate"] is not None)
    direction = ""
    rated = [t for t in trend if t["rate"] is not None and t["good"] + t["bad"] >= 5]
    if len(rated) >= 2:
        delta = rated[-1]["rate"] - rated[0]["rate"]
        direction = (f" I'm improving (+{delta:.0%} since {rated[0]['weeks_ago']} weeks ago)." if delta > 0.02 else
                     f" I got worse ({delta:.0%}) - tell me what's going wrong." if delta < -0.02 else " Steady.")
    message = (f"From {report['turns']} requests: {report['success_rate']:.0%} went right ({weekly}).{direction} "
               f"I know {report['skills']} skills and {report['lessons']} lessons from your corrections"
               + (f"; {report['retired']} that didn't help were retired" if report["retired"] else "") + ".")
    return ToolResult.success(message, **report)


def _forget_risk(args: LearningArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.DANGEROUS if args.action == "forget" else RiskLevel.SAFE


async def tune_ai(_: NoArgs, ctx: ToolContext) -> ToolResult:
    """Starts the self-improvement test in the background (it can take minutes) and reports when done."""
    if ctx.paths is None:
        raise ToolError("This needs Jarvis's data folder.", "unavailable")
    database = _db(ctx)
    from app.agent import self_improve

    cases = Growth(database).cases()
    if len(cases) < self_improve.MIN_CASES:
        return ToolResult.success(f"Not yet: I can only test myself on your real requests, and I have {len(cases)} "
                                  f"with a known right answer (I need {self_improve.MIN_CASES}). Keep using me.",
                                  cases=len(cases))
    registry = ctx.executor.registry if ctx.executor is not None else None
    if registry is None:
        from app.tools.catalog import build_default_registry

        registry = build_default_registry()

    async def job() -> None:
        from app.core import resources

        await resources.scheduler().run(resources.P3, "self-improvement test", _improve, vram_gb=1.0)

    async def _improve() -> None:
        from app.agent.improve_job import improve_now

        try:
            outcome = await improve_now(ctx.settings, ctx.paths, database, registry)
            message = outcome.message + (" Restart me to use it (say 'restart yourself')." if outcome.changed else "")
        except Exception as exc:
            message = f"Self-improvement couldn't run: {exc}"
        if ctx.notify is not None:
            ctx.notify("Self-improvement", message)

    asyncio.get_running_loop().create_task(job())
    return ToolResult.success(f"Started: I'm testing better set-ups on {len(cases)} of your real requests. I'll keep a "
                              "change only if it scores better, and tell you the result in a few minutes.",
                              cases=len(cases), started=True)


TOOLS = (
    Tool("learning", "What Jarvis has learned from using it (skills, lessons from corrections), whether it is "
         "improving week by week, or forget all of it ('what have you learned', 'are you getting better').",
         learning, LearningArgs, RiskLevel.SAFE, "memory", risk=_forget_risk,
         confirmation=lambda a, _: "Erase everything I learned from use (skills, lessons, request history)?"),
    Tool("tune_ai", "Test better set-ups for Jarvis's own AI (another installed model, a more exact setting) "
         "on the user's real requests and keep one only if it scores better ('tune your AI', 'make yourself "
         "smarter', 'upgrade your brain').", tune_ai, NoArgs, RiskLevel.MODERATE, "memory", timeout=20,
         confirmation=lambda a, _: "Run the self-improvement test now? It uses the AI for a few minutes and may "
                                   "switch me to a better-scoring set-up."),
)
