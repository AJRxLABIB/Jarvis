"""privacy_zones: which sensitive programs make Jarvis stop capturing / remembering / syncing / letting a phone control the screen (app/core/privacy_zones.py).
It can only add restrictions or switch a zone off with your say-so; it can never grant anything."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core import privacy_zones as Z
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class ZoneArgs(ToolArgs):
    action: Literal["status", "add", "remove", "turn_off", "turn_on", "test"] = Field(
        default="status", description="status = zones and whether one is active now; add / remove = your own zone; turn_off / turn_on = a built-in zone; "
                                      "test = would this program / window title be a zone?")
    name: str = Field(default="", max_length=40, description="For add / remove: the zone's name. For turn_off / turn_on: passwords, banking, private_browsing or meetings")
    titles: list[str] = Field(default_factory=list, max_length=10, description="For add: words in a window title that put you in the zone")
    programs: list[str] = Field(default_factory=list, max_length=10, description="For add: program names (e.g. 'veracrypt')")
    restrict: list[str] = Field(default_factory=list, max_length=5, description=f"For add: which of {', '.join(Z.KINDS)} to restrict (empty = all)")
    process: str = Field(default="", max_length=60, description="For test: a program name")
    title: str = Field(default="", max_length=120, description="For test: a window title")


def _kv(ctx: ToolContext):
    from app.memory.settings_store import SettingsStore

    if ctx.database is None:
        raise ToolError("Privacy zones need the local database.", "no_database")
    return SettingsStore(ctx.database)


def privacy_zones(args: ZoneArgs, ctx: ToolContext) -> ToolResult:
    kv = _kv(ctx)
    try:
        if args.action == "add":
            text = Z.add_custom(kv, args.name, args.titles, args.programs, args.restrict)
        elif args.action == "remove":
            text = "Zone removed." if Z.remove_custom(kv, args.name) else "No such custom zone."
        elif args.action in ("turn_off", "turn_on"):
            text = Z.set_builtin(kv, args.name, args.action == "turn_on")
        elif args.action == "test":
            cfg = Z.load(kv)
            hits = Z.active(cfg, Z.Facts(args.process, args.title))
            text = ("; ".join(f"{h.zone.label}: restricts {', '.join(sorted(h.zone.restrict))} ({h.why})" for h in hits)) or "Not in any zone."
            return ToolResult.success(text, zones=[h.zone.key for h in hits])
        else:
            Z.configure(kv)
            facts = Z.current_facts(0)
            return ToolResult.success(Z.describe(Z.load(kv), facts), active=[h.zone.key for h in Z.active(Z.load(kv), facts)])
    except ValueError as exc:
        raise ToolError(str(exc), "bad_zone") from exc
    Z.configure(kv)
    Z.reset_cache()
    return ToolResult.success(text)


def _risk(args: ZoneArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "turn_off" else RiskLevel.SAFE


def _question(args: ZoneArgs, ctx: ToolContext) -> str:
    return f"Switch the '{args.name}' privacy zone OFF? Jarvis will stop restricting capture, memory, clipboard and remote control for it."


TOOLS: tuple[Tool, ...] = (
    Tool("privacy_zones", "Show or change privacy zones: password managers, banking, private browsing and meetings (and your own) make Jarvis stop capturing the "
         "screen, remembering, saving the clipboard and letting a phone control the screen while they are in front.", privacy_zones, ZoneArgs, RiskLevel.SAFE,
         "windows", timeout=20, risk=_risk, confirmation=_question),
)
