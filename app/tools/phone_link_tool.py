"""``phone_control``: do something on your paired phone (flashlight, ring it, open an app or a link, show a message)
or see its battery. Uses the signed JARVIS Mobile link; the phone must allow it in its own settings."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from app.remote.phone_link import PhoneLink
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class PhoneArgs(ToolArgs):
    action: Literal["status", "flashlight_on", "flashlight_off", "ring", "open_app", "open_link", "show_text"] = Field(
        description="status = battery/connection; ring = find my phone; open_app needs app; open_link needs url")
    app: str = Field(default="", max_length=60, description="App to open on the phone, e.g. 'WhatsApp'")
    url: str = Field(default="", max_length=1900, description="http(s) link to open on the phone")
    text: str = Field(default="", max_length=500, description="Message to show on the phone")
    phone: str = Field(default="", max_length=60, description="Which phone (name), if more than one is paired")


def _phone(ctx: ToolContext, wanted: str):
    from app.remote.mobile import MobileRegistry

    if ctx.database is None:
        raise ToolError("The phone link needs the local database.", "no_database")
    devices = [d for d in MobileRegistry(ctx.database).all() if d.trust == "trusted" and not d.revoked_at]
    if wanted:
        devices = [d for d in devices if wanted.lower() in d.name.lower()]
    if not devices:
        raise ToolError("No trusted phone is paired. Pair JARVIS Mobile first (Menu > Mobile devices).", "no_phone")
    link = PhoneLink(ctx.database)
    devices.sort(key=lambda d: link.state(d.id).get("seen", ""), reverse=True)
    return devices[0], link


async def phone_control(args: PhoneArgs, ctx: ToolContext) -> ToolResult:
    device, link = _phone(ctx, args.phone)
    state = link.state(device.id)
    if args.action == "status":
        if not state:
            return ToolResult.success(f"{device.name} hasn't checked in yet - open JARVIS Mobile on it.", phone=device.name)
        battery = state.get("battery")
        parts = [f"battery {battery}%" if isinstance(battery, int | float) else "battery unknown"]
        if state.get("charging"):
            parts.append("charging")
        parts.append(f"last seen {state.get('seen', '?')} (UTC)")
        if not state.get("control"):
            parts.append("control from the PC is OFF on the phone")
        return ToolResult.success(f"{device.name}: " + ", ".join(parts) + ".", phone=device.name, state=state)
    if state and not state.get("control"):
        raise ToolError(f"{device.name} doesn't allow control from the PC. On the phone: JARVIS Mobile > Settings > "
                        "'Let my PC control this phone'.", "not_allowed")
    try:
        command_id = link.queue(device.id, args.action, {"app": args.app, "url": args.url, "text": args.text})
    except ValueError as exc:
        raise ToolError(str(exc), "bad_request") from exc
    outcome = await asyncio.to_thread(link.wait, command_id, 8.0)
    if outcome["status"] == "done":
        return ToolResult.success(outcome["result"] or f"Done on {device.name}.", phone=device.name)
    if outcome["status"] == "failed":
        raise ToolError(f"{device.name} couldn't do it: {outcome['result'] or 'no reason given'}.", "phone_failed")
    return ToolResult.success(f"Sent to {device.name}; it will do it when JARVIS Mobile checks in (open the app). "
                              "I couldn't confirm it yet.", phone=device.name, pending=True, verified=False)


def _risk(args: PhoneArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "open_link" else RiskLevel.SAFE


TOOLS = (
    Tool("phone_control", "Your paired phone: battery status, flashlight on/off, ring it (find my phone), open an app "
         "or a link on it, show a message on it.", phone_control, PhoneArgs, RiskLevel.SAFE, "phone", timeout=20,
         risk=_risk),
)
