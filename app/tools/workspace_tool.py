"""Workspaces as a tool (rules in app/core/workspaces.py): one named place joining a project folder, files, a window layout, a goal and
notes, with a preview of what restoring would do and report of what is missing."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class WorkspaceArgs(ToolArgs):
    action: Literal["list", "save", "show", "restore", "delete", "ignore", "unignore"] = Field(
        default="list",
        description="list; save = create/update (folder, files, layout, goal, notes); show = preview what restoring would do and what is "
                    "missing; restore = open its folder/files and bring back its layout (asks first; uses the normal tools); delete; "
                    "ignore / unignore = switch one signal (files, layout, goal, mission) off or on for this workspace")
    name: str = Field(default="", max_length=40, description="The workspace's name, e.g. 'thesis'")
    folder: str = Field(default="", max_length=400, description="For save: the project folder")
    files: list[str] = Field(default_factory=list, max_length=12, description="For save: files to open (replaces the list when given)")
    layout: str = Field(default="", max_length=40, description="For save: the name of a saved window layout")
    goal: str = Field(default="", max_length=120, description="For save: an active goal's title or number")
    notes: str = Field(default="", max_length=500, description="For save: a note shown with the workspace")
    signal: str = Field(default="", max_length=10, description="For ignore / unignore: files, layout, goal or mission")


async def workspace_tool(args: WorkspaceArgs, ctx: ToolContext) -> ToolResult:
    from app.core import workspaces as W

    if ctx.database is None:
        raise ToolError("Workspaces need the local database.", "no_database")
    if args.action == "list":
        items = W.load(ctx.database)
        if not items:
            return ToolResult.success("No workspaces yet. Save one with a folder, files, a layout or a goal.", workspaces=[])
        return ToolResult.success("\n".join(f"{e['name']}: {e.get('folder') or 'no folder'}"
                                            f"{', layout ' + e['layout'] if e.get('layout') else ''}"
                                            f"{', ' + str(len(e.get('files', []))) + ' file(s)' if e.get('files') else ''}" for e in items.values()),
                                  workspaces=sorted(items))
    if not args.name.strip():
        raise ToolError("Which workspace?", "missing_name")
    if args.action == "save":
        e = W.save(ctx, args.name, folder=args.folder, files=args.files or None, layout=args.layout, goal=args.goal, notes=args.notes)
        return ToolResult.success(f"Saved the workspace '{e['name']}'.\n{W.preview(ctx, args.name).describe()}", name=e["name"])
    if args.action == "show":
        pv = W.preview(ctx, args.name)
        return ToolResult.success(pv.describe(), missing=len(pv.missing), steps=len(pv.steps))
    if args.action == "delete":
        return ToolResult.success("Deleted." if W.delete(ctx.database, args.name) else "There was no such workspace.")
    if args.action in ("ignore", "unignore"):
        now = W.set_ignored(ctx.database, args.name, args.signal, args.action == "ignore")
        return ToolResult.success(f"Switched off for this workspace: {', '.join(now) or 'nothing'}.", ignored=now)
    pv, results = await W.restore(ctx, args.name)
    lines = [f"{'OK  ' if ok else 'FAIL'} {tool}: {msg}" for tool, ok, msg in results]
    failed = [t for t, ok, _ in results if not ok]
    text = pv.describe() + "\n" + "\n".join(lines)
    return ToolResult.success(text, restored=len(results) - len(failed), failed=failed, missing=len(pv.missing)) if not failed else \
        ToolResult.failure("partly_restored", text, restored=len(results) - len(failed), failed=failed)


def _risk(args: WorkspaceArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("restore", "delete") else RiskLevel.SAFE


def _question(args: WorkspaceArgs, _ctx: ToolContext) -> str:
    if args.action == "delete":
        return f"Delete the workspace '{args.name}'? (Only the saved setup; no files are touched.)"
    return f"Restore the workspace '{args.name}': open its folder and files and bring back its window layout?"


TOOLS: tuple[Tool, ...] = (
    Tool("workspace", "Named workspaces that join a project folder, files, a saved window layout, a goal and notes. 'show' previews what "
         "restoring would open and reports what is missing; 'restore' opens it through the normal tools (asks first); signals can be "
         "switched off per workspace.",
         workspace_tool, WorkspaceArgs, RiskLevel.SAFE, "system", timeout=60, risk=_risk, confirmation=_question),
)
