"""``location_status`` (saved places and the last events, no coordinates shown) and ``manage_places`` (add / remove a place, add / remove a rule). Places and rules live in
the settings store; events come from the phone, which evaluates the places itself (core/geofence.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core import geofence as G
from app.memory.settings_store import SettingsStore
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult

from app.core.location_events import KEY


def load_hub(ctx: ToolContext) -> G.Hub:
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    return G.Hub.load(SettingsStore(ctx.database).get(KEY))


def save_hub(ctx: ToolContext, hub: G.Hub) -> None:
    SettingsStore(ctx.database).set(KEY, hub.dump())


def location_status(_: NoArgs, ctx: ToolContext) -> ToolResult:
    hub = load_hub(ctx)
    return ToolResult.success(hub.describe() + ("\nRules: " + "; ".join(f"{r.on} {hub.places[r.place_id].name} -> {r.say or 'suggest profile ' + r.profile}" for r in hub.rules) if hub.rules else ""),
                              places=len(hub.places))


class PlaceArgs(ToolArgs):
    action: Literal["add_place", "remove_place", "add_rule", "remove_rules", "send_to_phone"] = Field(description="What to do")
    id: str = Field(default="", max_length=64, description="A short id for the place (letters, digits, - _), e.g. 'home' (not needed for send_to_phone)")
    phone: str = Field(default="", max_length=60, description="send_to_phone: which paired phone (part of its name); empty = the one seen last")
    name: str = Field(default="", max_length=60, description="add_place: the place's name")
    latitude: float | None = Field(default=None, ge=-90, le=90, description="add_place: latitude in degrees")
    longitude: float | None = Field(default=None, ge=-180, le=180, description="add_place: longitude in degrees")
    radius_m: float = Field(default=150.0, ge=50, le=5000, description="add_place: radius in metres (at least 50: a phone's own error is about that large)")
    on: Literal["enter", "exit", "dwell"] = Field(default="enter", description="add_rule: when it fires")
    say: str = Field(default="", max_length=200, description="add_rule: the reminder text ('{place}' is replaced by the place name)")
    profile: str = Field(default="", max_length=40, description="add_rule: a profile name to SUGGEST (it is not applied automatically)")


async def _send_to_phone(args: PlaceArgs, ctx: ToolContext, hub: G.Hub) -> ToolResult:
    import asyncio

    from app.remote.phone_link import check_action
    from app.tools.phone_link_tool import _phone

    if not hub.places:
        raise ToolError("There are no saved places to send yet.", "invalid_arguments")
    device, link = _phone(ctx, args.phone)
    payload = [{"id": p.id, "name": p.name, "lat": p.lat, "lon": p.lon, "radius_m": p.radius_m} for p in hub.places.values()]
    try:
        check_action("set_places", {"places": payload})
        command_id = link.queue(device.id, "set_places", {"places": payload})
    except ValueError as exc:
        raise ToolError(str(exc), "invalid_arguments") from exc
    outcome = await asyncio.to_thread(link.wait, command_id, 12.0)
    note = " On the phone: JARVIS Mobile > Command center > Place alerts, switch it on and allow Location (all the time) so it can watch with the app closed."
    if outcome["status"] == "done":
        return ToolResult.success(f"{device.name}: {outcome['result']}" + note, phone=device.name, places=len(payload))
    if outcome["status"] == "unsupported":
        raise ToolError(f"{device.name}'s JARVIS Mobile is older than 1.6 and can't watch places; install the new app.", "unsupported")
    return ToolResult.success(f"Sent {len(payload)} place(s) to {device.name}; it takes them at its next check-in." + note,
                              phone=device.name, pending=True, verified=False, command_id=command_id)


async def manage_places(args: PlaceArgs, ctx: ToolContext) -> ToolResult:
    hub = load_hub(ctx)
    if args.action == "send_to_phone":
        return await _send_to_phone(args, ctx, hub)
    if not args.id:
        raise ToolError("Give the place an id.", "invalid_arguments")
    try:
        if args.action == "add_place":
            if args.latitude is None or args.longitude is None or not args.name.strip():
                raise ValueError("add_place needs a name, latitude and longitude")
            hub.add_place(G.Place(args.id, args.name, args.latitude, args.longitude, args.radius_m))
            msg = f"Saved place '{args.name}' (radius {args.radius_m:.0f} m). Coordinates are stored on this PC and are not shown in replies."
        elif args.action == "remove_place":
            if not hub.remove_place(args.id):
                raise ValueError("no such place")
            msg = f"Removed the place '{args.id}' and its rules."
        elif args.action == "add_rule":
            hub.add_rule(G.Rule(args.id, args.on, args.say, args.profile))
            msg = f"Rule saved: when you {args.on} {hub.places[args.id].name}, " + (f"remind: {args.say}" if args.say else f"suggest the '{args.profile}' profile") + "."
        else:
            before = len(hub.rules)
            hub.rules = [r for r in hub.rules if r.place_id != args.id]
            msg = f"Removed {before - len(hub.rules)} rule(s) for '{args.id}'."
    except ValueError as exc:
        raise ToolError(str(exc), "invalid_arguments") from exc
    save_hub(ctx, hub)
    return ToolResult.success(msg, places=len(hub.places))


TOOLS: tuple[Tool, ...] = (
    Tool("location_status", "Show the saved places (names and radii only, never coordinates), the location rules and the last place event ('where are my saved places', 'location rules').",
         location_status, NoArgs, RiskLevel.SAFE, "memory", timeout=10),
    Tool("manage_places", "Add or remove a saved place, add / remove a rule that says something when you enter, leave or stay at it, or send the saved places to the phone so it watches them ('save home as a place', 'when I arrive home remind me', 'send my places to my phone').",
         manage_places, PlaceArgs, RiskLevel.MODERATE, "memory", timeout=10),
)
