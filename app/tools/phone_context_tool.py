"""``phone_context``: what the paired phone last said about its surroundings (network, headphones, orientation, motion, light), with its age. Read-only (app/core/phone_context.py)."""

from __future__ import annotations

from app.core import phone_context as PC
from app.remote.phone_link import PhoneLink
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolContext, ToolError, ToolResult


def phone_context(_: NoArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    with ctx.database.connect() as conn:
        devices = [(r["id"], r["name"]) for r in conn.execute("SELECT id, name FROM mobile_devices WHERE trust IN ('trusted', 'restricted')")]
    if not devices:
        return ToolResult.success("No trusted phone is paired.", devices=0)
    link = PhoneLink(ctx.database)
    lines, offers = [], []
    for device_id, name in devices:
        state = link.state(device_id)
        lines.append(PC.describe(name, state))
        offers += PC.suggestions(state)
    text = "\n".join(lines) + ("\nPossible: " + " ".join(dict.fromkeys(offers)) if offers else "")
    return ToolResult.success(text, devices=len(devices))


TOOLS: tuple[Tool, ...] = (
    Tool("phone_context", "What the paired phone says about its surroundings (Wi-Fi or mobile data, headphones, orientation, still or moving, light) and how old that is "
         "('is my phone on Wi-Fi', 'does my phone have headphones connected', 'is my phone moving').", phone_context, NoArgs, RiskLevel.SAFE, "windows", timeout=10),
)
