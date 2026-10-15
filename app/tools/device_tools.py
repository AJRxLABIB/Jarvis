"""Your devices: what they are, what they can really do, is it on, wake it (app/core/devices.py has the rules)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.devices import DeviceRegistry, wake, wake_setup
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _registry(ctx: ToolContext) -> DeviceRegistry:
    if ctx.database is None:
        raise ToolError("The device list needs the database.", "no_database")
    registry = DeviceRegistry(ctx.database)
    if not any(d.is_self for d in registry.all()):
        registry.register_self()
    return registry


def list_devices(_: NoArgs, ctx: ToolContext) -> ToolResult:
    devices = _registry(ctx).all()
    return ToolResult.success("; ".join(d.describe() for d in devices) + ".",
                              devices=[{"name": d.name, "kind": d.kind, "state": d.last_state, "self": d.is_self}
                                       for d in devices])


class DeviceArgs(ToolArgs):
    name: str | None = Field(default=None, max_length=60, description="The device; empty = this PC")


def device_info(args: DeviceArgs, ctx: ToolContext) -> ToolResult:
    registry = _registry(ctx)
    device = registry.find(args.name)
    if device.is_self:
        device = registry.register_self()  # re-detect: capabilities are read from Windows now
    caps = "; ".join(f"{c.name.replace('_', ' ')}: {c.status}" + (f" ({c.detail})" if c.detail else "")
                     for c in device.capabilities)
    message = f"{device.describe()}. Capabilities - {caps}."
    if device.is_self:
        message += " " + wake_setup(device.capabilities, {"name": device.name, "mac": device.mac,
                                                          "address": device.address})
    return ToolResult.success(message, mac=device.mac, address=device.address,
                              capabilities={c.name: c.status for c in device.capabilities})


class AddDeviceArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=60, description="What you call it, e.g. 'desktop'")
    mac: str = Field(min_length=12, max_length=17, description="Its MAC address, e.g. 3C-52-82-1A-2B-3C")
    address: str = Field(default="", max_length=63, description="Its IP address or name on your network (to check it)")
    kind: Literal["computer", "laptop", "desktop", "server", "tv", "other"] = Field(default="computer",
                                                                                   description="What kind of device")


def add_device(args: AddDeviceArgs, ctx: ToolContext) -> ToolResult:
    device = _registry(ctx).add(args.name, args.mac, args.address, args.kind)
    tip = "" if args.address else " Add its address too, so I can check whether it really woke up."
    return ToolResult.success(f"Added {device.name} ({device.mac}). Wake-on-LAN: not tried yet.{tip}")


def remove_device(args: DeviceArgs, ctx: ToolContext) -> ToolResult:
    registry = _registry(ctx)
    device = registry.find(args.name)
    registry.remove(device)
    return ToolResult.success(f"Removed {device.name} from your devices.")


def check_device(args: DeviceArgs, ctx: ToolContext) -> ToolResult:
    from app.core.devices import reachable

    registry = _registry(ctx)
    device = registry.find(args.name)
    if device.is_self:
        return ToolResult.success(f"{device.name} is on - I'm running on it.", online=True)
    if not device.address:
        return ToolResult.failure("no_address", f"I don't know {device.name}'s network address, so I can't check it.")
    port = reachable(device.address)
    registry.set_state(device, "online" if port else "not answering", seen=port is not None)
    if port:
        return ToolResult.success(f"{device.name} is on (it answers on the network).", online=True)
    return ToolResult.success(f"{device.name} isn't answering on the network (off, asleep, or blocking checks).",
                              online=False)


def wake_device(args: DeviceArgs, ctx: ToolContext) -> ToolResult:
    registry = _registry(ctx)
    device = registry.find(args.name)
    report = wake(device, registry, jarvis_port=ctx.settings.remote_port if ctx.settings.remote_enabled else None)
    data = {"sent": report.sent, "online": report.online, "jarvis_ready": report.jarvis_ready}
    if report.online:
        return ToolResult.success(report.message(device.name), **data, verified=True)
    return ToolResult.failure("not_verified", report.message(device.name), **data)


TOOLS: tuple[Tool, ...] = (
    Tool("list_devices", "The user's devices (this PC and ones they added) with their state.", list_devices, NoArgs,
         RiskLevel.SAFE, "phone"),
    Tool("device_info", "What a device can really do (detected, not assumed): lock / sleep / restart / shutdown / "
         "hibernate, remote wake from sleep or shutdown, and for this PC exactly what remote power-on needs. Use for "
         "'can you turn on my laptop remotely?'.", device_info, DeviceArgs, RiskLevel.SAFE, "phone", timeout=60),
    Tool("add_device", "Add a device (name, MAC address, network address) so Jarvis can check it and wake it.",
         add_device, AddDeviceArgs, RiskLevel.SAFE, "phone"),
    Tool("remove_device", "Remove a device from the list.", remove_device, DeviceArgs, RiskLevel.MODERATE, "phone",
         confirmation=lambda a, c: f"Remove {a.name} from your devices?"),
    Tool("check_device", "Is a device on right now (does it answer on the network)?", check_device, DeviceArgs,
         RiskLevel.SAFE, "phone", timeout=20),
    Tool("wake_device", "Wake another device with Wake-on-LAN and CHECK that it came online (never claims it's on "
         "without seeing it).", wake_device, DeviceArgs, RiskLevel.SAFE, "phone", timeout=240),
)
