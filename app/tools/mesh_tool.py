"""device_mesh: every device JARVIS knows or can see on the local network, with trust and availability (app/core/device_mesh.py)."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class MeshArgs(ToolArgs):
    action: Literal["list", "discover", "status"] = Field(
        default="list", description="list = every known device; discover = also look for JARVIS devices on the local network (never trusts them); "
                                    "status = one device in detail")
    device: str = Field(default="", max_length=60, description="For status: the device name")


async def device_mesh(args: MeshArgs, ctx: ToolContext) -> ToolResult:
    from app.core import device_mesh as M

    if ctx.database is None:
        raise ToolError("The local database isn't available.", "no_database")
    found: list = []
    if args.action == "discover":
        from app.remote import discovery

        found = await asyncio.to_thread(discovery.scan, 2.0)
    nodes = await asyncio.to_thread(M.snapshot, ctx.database, found)
    if args.action == "status":
        key = args.device.lower().strip()
        hit = [n for n in nodes if key and (key == n.name.lower() or key in n.name.lower())]
        if len(hit) != 1:
            raise ToolError(f"Which device? {', '.join(n.name for n in nodes) or 'none known'}.", "ambiguous" if hit else "not_found")
        n = hit[0]
        detail = [n.line()] + [f"  {name}: {status}" for name, status in n.abilities] + [f"  {r}" for r in n.running]
        return ToolResult.success("\n".join(detail), **n.to_dict())
    text = M.render(nodes)
    if args.action == "discover":
        extra = sum(1 for n in nodes if n.kind == "discovered")
        text += f"\nLooked on the local network: {len(found)} JARVIS device(s) answered, {extra} not paired." if found else \
            "\nLooked on the local network: nothing answered (the other device may not be running JARVIS, or this network blocks multicast)."
    return ToolResult.success(text, devices=[n.to_dict() for n in nodes], discovered=len(found))


TOOLS: tuple[Tool, ...] = (
    Tool("device_mesh", "List every device Jarvis knows (this PC, other computers, paired phones) with connection, trust, battery, what each can do and what is "
         "waiting on it; 'discover' also looks for JARVIS devices on the local network (found devices are NEVER trusted until paired). "
         "For 'check all my devices', 'which devices are connected', 'what is on my network'.", device_mesh, MeshArgs, RiskLevel.SAFE, "system", timeout=30),
)
