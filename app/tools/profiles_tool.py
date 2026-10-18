"""Switch between named sets of everyday settings ("meeting mode", "night", "performance"). See app/config/profiles.py."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.config import profiles as prof
from app.memory.settings_store import SettingsStore
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class ProfileArgs(ToolArgs):
    action: Literal["list", "show", "save", "apply", "undo", "delete"] = Field(
        description="list profiles; show what one contains; save = store the CURRENT everyday settings under a name; "
                    "apply = switch to a profile; undo = go back to the settings that the last apply replaced; delete")
    name: str = Field(default="", max_length=40, description="Profile name, e.g. meeting, night, performance, or your own")


def _store(ctx: ToolContext) -> prof.ProfileStore:
    if ctx.database is None:
        raise ToolError("Profiles need the database.", "no_database")
    return prof.ProfileStore(SettingsStore(ctx.database))


def _wrap(call):
    try:
        return call()
    except prof.ProfileError as exc:
        raise ToolError(str(exc), "profile") from exc


def _risk(args: ProfileArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("apply", "undo", "delete") else RiskLevel.SAFE


def _question(args: ProfileArgs, ctx: ToolContext) -> str:
    if args.action == "delete":
        return f"Delete the profile '{args.name}'?"
    try:
        store = _store(ctx)
        target = store.get(prof.PREVIOUS if args.action == "undo" else args.name)
        return f"Switch to {'the previous settings' if args.action == 'undo' else repr(args.name)}? " + \
               prof.describe_diff(prof.diff(ctx.settings, target))
    except (ToolError, prof.ProfileError) as exc:
        return f"Apply that profile? ({exc})"


def settings_profile(args: ProfileArgs, ctx: ToolContext) -> ToolResult:
    store = _store(ctx)
    if args.action == "list":
        names = store.names()
        return ToolResult.success("Profiles: " + ", ".join(names) + ".", profiles=names)
    if args.action == "undo":
        target = _wrap(lambda: store.get(prof.PREVIOUS))
        label = "your previous settings"
    else:
        if not args.name:
            raise ToolError("Which profile?", "missing_name")
        if args.action == "save":
            saved = _wrap(lambda: store.save(args.name, prof.capture(ctx.settings)))
            return ToolResult.success(f"Saved your current everyday settings as '{saved}'. "
                                      "Secrets, paths, devices and security settings are never part of a profile.")
        if args.action == "delete":
            _wrap(lambda: store.delete(args.name))
            return ToolResult.success(f"Deleted the profile '{args.name}'.")
        target = _wrap(lambda: store.get(args.name))
        label = f"'{args.name}'"
        if args.action == "show":
            return ToolResult.success(f"{label}: " + "; ".join(f"{k.replace('_', ' ')} = {v}" for k, v in target.items()),
                                      settings=target)
    changes = prof.diff(ctx.settings, target)
    if not changes:
        return ToolResult.success(f"Already using {label}; nothing to change.")
    apply = getattr(ctx, "apply_setting", None)
    if apply is None:
        raise ToolError("Profiles can be switched from the Jarvis window.", "unavailable")
    if args.action == "apply":                                    # remember what is being replaced, so "undo" works
        store.remember_previous({f: old for f, (old, _) in changes.items()})
    for field, (_, new) in changes.items():
        apply(field, new)
    return ToolResult.success(f"Switched to {label}. Changed: {prof.describe_diff(changes)}", changed=sorted(changes))


TOOLS: tuple[Tool, ...] = (
    Tool("settings_profile", "Switch between named sets of everyday settings (meeting, night, performance, or your own): "
         "list, show, save the current ones, apply, undo, delete. Never touches secrets, paths or security settings.",
         settings_profile, ProfileArgs, RiskLevel.SAFE, "system", timeout=20, risk=_risk, confirmation=_question),
)
