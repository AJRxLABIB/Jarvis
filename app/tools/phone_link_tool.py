"""``phone_control``: do something on your paired phone - flashlight, ring it, open an app / the gallery / camera /
messages / dialer or a link, show a message, start screen sharing, call someone, end a call, send an SMS, read the
latest messages, look up a contact - or see its battery. Uses the signed JARVIS Mobile link; the phone must allow it
in its own settings ("Let my PC control this phone"; calls and messages need "Allow calls and messages" too).
Calls and SMS always ask you first (DANGEROUS), reading messages / contacts asks unless you approved it (MODERATE)."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from app.remote.phone_link import PhoneLink
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class PhoneArgs(ToolArgs):
    action: Literal["status", "flashlight_on", "flashlight_off", "ring", "open_app", "open_link", "show_text",
                    "open_gallery", "open_camera", "open_messages", "open_dialer", "share_screen",
                    "call", "end_call", "send_sms", "read_messages", "find_contact", "cancel_pending",
                    "read_screen", "tap_text", "set_clipboard", "locate", "received_files", "lock_phone"] = Field(
        description="status = battery/connection; ring = find my phone; open_app needs app; open_link needs url; "
                    "share_screen = ask the phone to start sharing its screen; call / send_sms need number (a contact "
                    "name or a number); send_sms needs text; read_messages = latest SMS (count, optional from); "
                    "find_contact needs name; cancel_pending = withdraw requests the phone hasn't taken yet; "
                    "read_screen = the text on the phone's screen now; tap_text = tap the button/item with that "
                    "text (text); set_clipboard = put text on the phone's clipboard; locate = where the phone is; "
                    "received_files = files the phone sent to this PC; lock_phone = lock the phone's screen")
    number: str = Field(default="", max_length=60, description="Who to call / text: a contact name or a phone number")
    count: int = Field(default=5, ge=1, le=10, description="How many messages to read")
    sender: str = Field(default="", max_length=60, description="read_messages: only from this contact / number")
    name: str = Field(default="", max_length=60, description="find_contact: the contact's name")
    app: str = Field(default="", max_length=60, description="App to open on the phone, e.g. 'WhatsApp'")
    url: str = Field(default="", max_length=1900, description="http(s) link to open on the phone")
    text: str = Field(default="", max_length=4000,
                      description="Message to show / SMS text / words to tap / text for the phone's clipboard")
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
    if args.action == "received_files":
        from app.remote.phone_inbox import PhoneInbox

        if ctx.paths is None:
            raise ToolError("This needs Jarvis's data folder.", "unavailable")
        files = PhoneInbox(ctx.paths.data_dir).recent(10)
        if not files:
            return ToolResult.success("Nothing has come from the phone yet (on the phone: Share > JARVIS Mobile).")
        return ToolResult.success("From your phone: " + "; ".join(f.name for f in files) + f". Folder: {files[0].parent}",
                                  files=[str(f) for f in files])
    if args.action == "cancel_pending":
        withdrawn = link.cancel(device.id)
        return ToolResult.success(f"Withdrew {withdrawn} request(s) {device.name} hadn't taken yet." if withdrawn else
                                  f"Nothing was waiting for {device.name} (anything it already took can't be called back).",
                                  phone=device.name, cancelled=withdrawn)
    from app.remote.phone_link import COMMS

    if args.action in COMMS and state and state.get("app_version") and not state.get("comms"):
        raise ToolError(f"{device.name} doesn't allow calls and messages from the PC. On the phone: JARVIS Mobile > "
                        "Control > 'Allow calls and messages'.", "not_allowed")
    if state and not state.get("control"):
        raise ToolError(f"{device.name} doesn't allow control from the PC. On the phone: JARVIS Mobile > Settings > "
                        "'Let my PC control this phone'.", "not_allowed")
    try:
        command_id = link.queue(device.id, args.action, {"app": args.app, "url": args.url, "text": args.text,
                                                         "number": args.number, "count": args.count,
                                                         "from": args.sender, "name": args.name})
    except ValueError as exc:
        raise ToolError(str(exc), "bad_request") from exc
    outcome = await asyncio.to_thread(link.wait, command_id, 12.0)
    if outcome["status"] == "done":
        return ToolResult.success(outcome["result"] or f"Done on {device.name}.", phone=device.name)
    if outcome["status"] == "unsupported":
        raise ToolError(f"{device.name}'s JARVIS Mobile app is too old for '{args.action}'. Rebuild and install the "
                        "new app (packaging\\build_android.ps1, then MENU > Mobile devices > Install).", "unsupported")
    if outcome["status"] == "failed":
        raise ToolError(f"{device.name} couldn't do it: {outcome['result'] or 'no reason given'}.", "phone_failed")
    return ToolResult.success(f"Sent to {device.name}; it will do it when JARVIS Mobile checks in (open the app, or "
                              "turn on 'Stay connected'). I couldn't confirm it yet - say 'cancel the phone request' "
                              "to withdraw it.", phone=device.name, pending=True, verified=False, command_id=command_id)


def _risk(args: PhoneArgs, _ctx: ToolContext) -> RiskLevel:
    if args.action in ("call", "send_sms", "lock_phone"):
        return RiskLevel.DANGEROUS            # reaches another person / shuts you out of the phone: always asks, on screen
    if args.action in ("open_link", "read_messages", "find_contact", "read_screen", "tap_text", "locate"):
        return RiskLevel.MODERATE             # private data / outside content
    return RiskLevel.SAFE


def _confirmation(args: PhoneArgs, _ctx: ToolContext) -> str:
    if args.action == "call":
        return f"Call {args.number} from your phone?"
    if args.action == "send_sms":
        return f"Send an SMS from your phone to {args.number}: \"{args.text}\"?"
    if args.action == "read_messages":
        return f"Read your latest {args.count} SMS" + (f" from {args.sender}" if args.sender else "") + " (codes hidden)?"
    if args.action == "find_contact":
        return f"Look up {args.name} in your phone's contacts?"
    if args.action == "read_screen":
        return "Read what's on your phone's screen right now (passwords are never read)?"
    if args.action == "tap_text":
        return f"Tap '{args.text}' on your phone's screen?"
    if args.action == "lock_phone":
        return "Lock your phone's screen now?"
    if args.action == "locate":
        return "Ask your phone where it is (its last known location)?"
    return f"Do '{args.action}' on your phone?"


TOOLS = (
    Tool("phone_control", "Your paired phone: battery status, flashlight on/off, ring it (find my phone), open an app, "
         "the gallery, camera, messages, dialer or a link on it, show a message on it, start sharing its screen, "
         "call someone, end a call, send an SMS, read the latest SMS, find a contact's number.", phone_control,
         PhoneArgs, RiskLevel.SAFE, "phone", timeout=25, risk=_risk, confirmation=_confirmation),
)
