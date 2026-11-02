"""Switch where sound plays: "put it on the speakers", "put it on Bluetooth", "use my headphones" (decision rules in app/core/audio_route.py).

Moves the Windows default playback device (music, videos, system sounds and calls follow it); the result is read back from Windows."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from app.core import audio_route as AR
from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

PREVIOUS: dict[str, str] = {}      # the output in use before the last switch ("put it back"); lives until Jarvis closes
ENV_FACTORY = None      # tests replace this; the default builds the Windows environment


def _env():
    if ENV_FACTORY is not None:
        return ENV_FACTORY()
    from app.tools.audio_win import WindowsAudio

    return WindowsAudio()


class AudioArgs(ToolArgs):
    action: Literal["switch", "list", "status"] = Field(
        default="switch", description="switch = move the sound to another output; list = every active sound output; status = which one is in use now")
    target: str = Field(default="speakers", max_length=60,
                        description="For switch: speakers | headphones | bluetooth | connected (a connected / Bluetooth speaker) | previous (go back to the output used before) | or a device name such as 'JBL'")
    loud: bool = Field(default=False, description="For switch to speakers: also raise the volume to a clearly audible level (at least 70%)")


def _describe(devices: list[AR.Device]) -> str:
    return "\n".join(f"{'>' if d.is_default else ' '} {d.name}  [{d.kind}]" for d in devices) or "No active sound outputs."


def _switch(args: AudioArgs, env) -> ToolResult:
    target, hint = AR.parse_target(args.target)
    devices = env.devices()
    before = next((d.id for d in devices if d.is_default), "")
    if target == "previous":
        hint = PREVIOUS.get("id", "")
    decision = AR.decide(devices, target, hint, loud=args.loud)
    if decision.device is None:
        return ToolResult.failure("no_device", decision.message)
    notes: list[str] = []
    if not decision.already:
        env.set_default(decision.device.id)
        if before and before != decision.device.id:
            PREVIOUS["id"] = before
        now = env.default_id()
        if now != decision.device.id:        # what Windows really did, not what was asked
            return ToolResult.failure("not_verified", f"I asked Windows to use {decision.device.name}, but it is still using another output. "
                                                      "Another app may be holding the device in exclusive mode.")
    if decision.raise_volume_to is not None:
        try:
            level = env.volume(decision.device.id)
            if level < decision.raise_volume_to:
                env.set_volume(decision.device.id, decision.raise_volume_to)
                notes.append(f"Speaker volume raised from {level}% to {decision.raise_volume_to}%.")
        except Exception:
            notes.append("I could not read or change the speaker volume.")
    message = " ".join([decision.message, *notes]).strip()
    return ToolResult.success(message, device=decision.device.name, kind=decision.device.kind, fallback=decision.fallback, already=decision.already,
                              others=decision.alternatives)


async def audio_output(args: AudioArgs, ctx: ToolContext) -> ToolResult:
    if ENV_FACTORY is None:
        winapi.require_windows()
    env = _env()
    if args.action in ("list", "status"):
        devices = await asyncio.to_thread(env.devices)
        if args.action == "status":
            current = next((d for d in devices if d.is_default), None)
            return ToolResult.success(f"Sound is playing on {current.name}." if current else "I can't tell which sound output is in use.",
                                      device=current.name if current else "")
        return ToolResult.success(_describe(devices), devices=[d.name for d in devices])
    if args.action == "switch":
        return await asyncio.to_thread(_switch, args, env)
    raise ToolError("Unknown action.", "bad_action")


TOOLS: tuple[Tool, ...] = (
    Tool("audio_output", "Change where sound plays: put it on the PC speakers, headphones, a Bluetooth / connected speaker, or a named device. If the "
         "wanted device is not connected it plays on the PC speakers. Also lists the active sound outputs.", audio_output, AudioArgs, RiskLevel.SAFE,
         "windows", timeout=30),
)
