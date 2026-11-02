"""Watch a window or the screen for a change or a phrase (rules in app/core/screen_watch.py): explicit, visible, time-limited, private
(blocked-window list applies on every look), cheap (adaptive polling, OCR only after a change) and stoppable at any time."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from app.core import screen_watch as SW
from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class WatchArgs(ToolArgs):
    action: Literal["start", "list", "pause", "resume", "stop", "stop_all"] = Field(
        default="list",
        description="start = begin watching (asks first: it looks at the screen while running); list = what is being watched; pause / resume / "
                    "stop one watch; stop_all = end every watch")
    window: str = Field(default="", max_length=80, description="For start: the window to watch (an app name or part of its title); empty = the whole screen")
    condition: Literal["change", "text", "assist"] = Field(default="change", description="For start: change = tell me when it changes; text = tell me when a phrase appears; assist = watch what I am doing and help (offers a tip when something needs it)")
    phrase: str = Field(default="", max_length=120, description="For start with condition=text: the words to wait for")
    minutes: int = Field(default=SW.DEFAULT_MINUTES, ge=1, le=SW.MAX_MINUTES, description="For start: how long to watch (it stops by itself)")
    repeat: bool = Field(default=False, description="For start: keep alerting (with a cooldown) instead of stopping after the first alert")
    watch_id: int = Field(default=0, ge=0, le=999, description="For pause / resume / stop: the watch number (from list)")


ASSIST_PROMPT = ("You are quietly helping the person whose screen this is. In one or two short sentences say what they seem to be doing and, ONLY if you "
                 "can see a clear problem (an error, a mistake, a stuck step) or a genuinely useful next step, give one concrete tip. If nothing needs "
                 "help, reply with exactly NOTHING. Never read out passwords, keys or private messages.")


def _observer(ctx: ToolContext, assist: bool = False):
    from app.vision import capture as cap
    from app.vision.ocr import create_ocr

    async def observe(target: str) -> SW.Observation:
        def look():
            if not ctx.settings.screen_capture_enabled:
                raise ToolError("Screen capture was turned off in Settings, so the watch stopped.", "disabled")
            try:
                got = cap.capture(ctx, window=target or None, target="window" if target else "screen")
            except ToolError as exc:
                if exc.code in ("blocked", "blocked_window", "private"):
                    return SW.Observation(blocked=str(exc))
                raise
            thumb = got.image.convert("L").resize((320, max(1, 320 * got.image.size[1] // max(1, got.image.size[0]))))
            return got, thumb

        result = await asyncio.to_thread(look)
        if isinstance(result, SW.Observation):
            return result
        got, thumb = result

        async def read() -> str:
            return (await create_ocr(ctx.settings).recognize(got.image)).text

        async def describe() -> str:
            from app.utils.redaction import redact
            from app.tools.vision import VISION_IMAGE_SIDE, VISION_SYSTEM

            model = await ctx.ai.vision_model()
            image = await asyncio.to_thread(cap.png_bytes, got.image, VISION_IMAGE_SIDE)
            response = await ctx.ai.vision_chat(model, VISION_SYSTEM, ASSIST_PROMPT, image)
            return redact(response.message.content.strip())[:600]

        return SW.Observation(thumb=thumb, title=got.source, read_text=read, describe=describe if assist else None)

    return observe


async def screen_watch(args: WatchArgs, ctx: ToolContext) -> ToolResult:
    from app.core import emergency

    mgr = SW.MANAGER
    if args.action == "list":
        active = mgr.watches
        return ToolResult.success("\n".join(w.describe() for w in active.values()) or "Nothing is being watched.", watches=[w.id for w in active.values()])
    if args.action == "stop_all":
        return ToolResult.success(f"Ended {mgr.stop()} watch(es). Screen monitoring is off.", ended=len(mgr.active()) == 0)
    if args.action in ("pause", "resume", "stop"):
        if not args.watch_id:
            raise ToolError("Which watch number? (say 'list watches')", "missing_id")
        if args.action == "stop":
            mgr.stop(args.watch_id)
        else:
            mgr.pause(args.watch_id, args.action == "pause")
        return ToolResult.success(mgr.get(args.watch_id).describe())
    winapi.require_windows()
    from app.vision.capture import require_enabled

    require_enabled(ctx)
    mgr.indicator = getattr(ctx, "on_screen_capture", None)
    assist = args.condition == "assist"
    if assist:
        if ctx.ai is None:
            raise ToolError("Screen assist needs a local vision model, and none is available.", "model_unavailable")
        try:
            await ctx.ai.vision_model()
        except Exception as exc:
            raise ToolError(f"Screen assist needs a vision model: {exc} (install one: 'ollama pull qwen2.5vl:3b').", "model_unavailable") from exc
    w = mgr.start(_observer(ctx, assist), ctx.notify or (lambda t, m: None), target=args.window, condition=args.condition, phrase=args.phrase,
                  minutes=args.minutes, repeat=args.repeat or assist, locked=lambda: emergency.STATE.flags.get("locked", False))
    if assist:
        w.cooldown = 45.0       # at most one tip every 45 s, and only after the screen has settled
    return ToolResult.success(f"Watching. {w.describe()} Say 'stop watching' at any time; the capture indicator stays on while I look.", watch_id=w.id)


def _risk(args: WatchArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("start", "resume") else RiskLevel.SAFE


def _question(args: WatchArgs, _ctx: ToolContext) -> str:
    what = f"the text '{args.phrase}'" if args.condition == "text" else "what you are doing, to help you (a local vision model looks at the screen after it settles; it speaks up only when it has something useful)" if args.condition == "assist" else "any change"
    return (f"Watch {args.window or 'your screen'} for {what} for up to {args.minutes} minutes? I will look every few seconds (slower when "
            "nothing changes), never save pictures, skip password managers and other blocked windows, and keep the capture indicator on.")


TOOLS: tuple[Tool, ...] = (
    Tool("screen_watch", "Watch a window or the screen and tell me when it changes or a phrase appears. Explicit, time-limited, visible "
         "(capture indicator), private (blocked windows are never captured), cheap (backs off while nothing changes) and stoppable.",
         screen_watch, WatchArgs, RiskLevel.SAFE, "system", timeout=30, risk=_risk, confirmation=_question),
)
