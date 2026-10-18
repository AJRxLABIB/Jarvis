"""``local_device``: switch Shelly / Tasmota plugs and WLED lights directly on your network (no cloud) - with the
device's own read-back as proof. See app/core/local_home.py."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.local_home import LocalDeviceError, LocalHome, read_state, set_state
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class LocalDeviceArgs(ToolArgs):
    action: Literal["on", "off", "toggle", "status", "brightness", "list"] = Field(
        description="on / off / toggle, status (ask the device), brightness (WLED lights), list the devices")
    device: str = Field(default="", max_length=40, description="The device's name, e.g. 'desk lamp'")
    brightness: int | None = Field(default=None, ge=1, le=100, description="Brightness in percent (WLED)")


def _home(ctx: ToolContext) -> LocalHome:
    if ctx.paths is None:
        raise ToolError("Local devices need Jarvis's config folder.", "unavailable")
    return LocalHome(ctx.paths.config_dir)


def local_device(args: LocalDeviceArgs, ctx: ToolContext) -> ToolResult:
    home = _home(ctx)
    try:
        if args.action == "list":
            devices = home.devices()
            if not devices:
                return ToolResult.success("No local devices yet. Add one in Menu > Device manager (Shelly, Tasmota "
                                          "or WLED on your network).", devices=[])
            return ToolResult.success("Local devices: " + "; ".join(d.describe() for d in devices) + ".",
                                      devices=[d.name for d in devices])
        device = home.find(args.device)
        if args.action == "status":
            state = read_state(device)
            extra = (f", brightness {round(state['brightness'] / 2.55)}%" if state.get("brightness") is not None
                     else "")
            return ToolResult.success(f"{device.name} is {'on' if state['on'] else 'off'}{extra} (the device says so).",
                                      state=state, verified=True)
        if args.action == "brightness":
            if args.brightness is None:
                raise ToolError("What brightness (1-100 %)?", "missing_value")
            outcome = set_state(device, on=True, brightness=round(args.brightness * 2.55))
        else:
            on = (not read_state(device)["on"]) if args.action == "toggle" else args.action == "on"
            outcome = set_state(device, on=on)
    except LocalDeviceError as exc:
        raise ToolError(str(exc), exc.code) from exc
    state = outcome["state"]
    now = f"{'on' if state['on'] else 'off'}" + (f" at {round(state['brightness'] / 2.55)}%"
                                                 if args.action == "brightness" and state.get("brightness") else "")
    if outcome["verified"]:
        return ToolResult.success(f"{device.name} is {now} - confirmed by the device.", state=state, verified=True)
    return ToolResult.failure("not_verified", f"I sent the command, but {device.name} reports it is {now}.",
                              state=state, verified=False)


def _risk(args: LocalDeviceArgs, ctx: ToolContext) -> RiskLevel:
    if args.action in ("status", "list"):
        return RiskLevel.SAFE
    try:
        return RiskLevel.MODERATE if _home(ctx).find(args.device).confirm else RiskLevel.SAFE
    except (LocalDeviceError, ToolError):
        return RiskLevel.MODERATE


TOOLS = (
    Tool("local_device", "Switch smart plugs / relays (Shelly, Tasmota) and WLED lights on this network directly "
         "(no cloud): on, off, toggle, brightness, status, list. Reports success only when the device confirms.",
         local_device, LocalDeviceArgs, RiskLevel.SAFE, "home", timeout=15, risk=_risk,
         confirmation=lambda a, _: f"Turn {a.action} {a.device}?"),
)
