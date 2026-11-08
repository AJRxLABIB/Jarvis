"""``sensors`` (read ESP32 / ESPHome / Zigbee2MQTT sensors over a local MQTT broker, honest about stale / unknown readings) and ``plugin_check``
(static capability scan of plugin files before you trust them). Both read-only."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from app.core import plugin_check, sensors as S
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult


class SensorArgs(ToolArgs):
    name: str = Field(default="", max_length=40, description="Part of a sensor's name (e.g. 'door', 'temperature'); empty = all sensors")
    wait_seconds: float = Field(default=3.0, ge=0.5, le=15.0, description="How long to listen to the broker for fresh messages")


def _cfg(ctx: ToolContext):
    if ctx.paths is None:
        raise ToolError("Sensors need Jarvis's config folder.", "unavailable")
    return ctx.paths.config_dir


async def sensors(args: SensorArgs, ctx: ToolContext) -> ToolResult:
    readings, problem = await asyncio.to_thread(S.read_all, _cfg(ctx), args.wait_seconds)
    if args.name:
        readings = [r for r in readings if args.name.lower() in r.sensor.name.lower()]
    if not readings:
        return ToolResult.success(problem or f"No sensor matches '{args.name}'.", readings=[])
    text = "\n".join(r.text() for r in readings) + (f"\n({problem})" if problem else "")
    return ToolResult.success(text, readings=[{"name": r.sensor.name, "status": r.status, "value": r.value} for r in readings])


async def plugin_check_tool(_: NoArgs, ctx: ToolContext) -> ToolResult:
    if ctx.paths is None:
        raise ToolError("Needs Jarvis's data folder.", "unavailable")
    rows = await asyncio.to_thread(plugin_check.check, ctx.paths.data_dir / "plugins")
    return ToolResult.success(plugin_check.describe(rows), plugins=[{"name": r["name"], "detected": sorted(r["detected"]), "undeclared": r["undeclared"]} for r in rows])


TOOLS: tuple[Tool, ...] = (
    Tool("sensors", "Read the room / door / motion / temperature sensors on the local MQTT broker (ESP32, ESPHome, Zigbee2MQTT). Reports stale or missing "
         "readings as unknown ('what is the temperature', 'is the office door open', 'check my sensors').", sensors, SensorArgs, RiskLevel.SAFE, "windows", timeout=25),
    Tool("plugin_check", "Scan the plugin files for network, process, native-code and dynamic-execution use before they are trusted ('check my plugins').",
         plugin_check_tool, NoArgs, RiskLevel.SAFE, "windows", timeout=20),
)
