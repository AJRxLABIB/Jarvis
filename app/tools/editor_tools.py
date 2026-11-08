"""VS Code and Office adapters.

* ``vscode``      - open a file at a line, open a folder, compare two files, list installed extensions. Uses VS Code's own ``code`` launcher through the
                    reviewed command executor with FIXED arguments (no shell, no user-supplied flags), on paths that pass the path policy.
* ``office_info`` - read-only structure of a .docx / .xlsx / .pptx (app/core/office_info.py); no Office needed, macros refused.
Editing Office files stays with the existing document / Excel tools and their previews."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class VSCodeArgs(ToolArgs):
    action: Literal["goto", "folder", "diff", "extensions"] = Field(
        default="goto", description="goto = open a file at a line; folder = open a folder; diff = compare two files; extensions = list installed extensions")
    path: str = Field(default="", max_length=400, description="File or folder (for goto / folder / diff: the first file)")
    other: str = Field(default="", max_length=400, description="For diff: the second file")
    line: int = Field(default=1, ge=1, le=1_000_000, description="For goto: line number")
    column: int = Field(default=1, ge=1, le=10_000, description="For goto: column")


def _executor(ctx: ToolContext):
    from app.tools import developer

    return developer._executor(ctx)


def build_argv(args: VSCodeArgs, resolve) -> list[str]:
    """The exact argument list (pure, tested). ``resolve(text)`` returns a checked path string."""
    if args.action == "extensions":
        return ["code", "--list-extensions"]
    if not args.path:
        raise ToolError("Which file or folder?", "missing_path")
    first = resolve(args.path)
    if args.action == "goto":
        return ["code", "-g", f"{first}:{args.line}:{args.column}"]
    if args.action == "folder":
        return ["code", first]
    if not args.other:
        raise ToolError("Which second file to compare with?", "missing_path")
    return ["code", "--diff", first, resolve(args.other)]


async def vscode(args: VSCodeArgs, ctx: ToolContext) -> ToolResult:
    policy = ctx.get_path_policy()
    argv = build_argv(args, lambda text: str(policy.resolve(text, must_exist=True)))
    from pathlib import Path

    base = Path(policy.resolve(args.path, must_exist=True)) if args.path else Path.home()
    cwd = base.parent if base.is_file() else base
    result = await asyncio.to_thread(_executor(ctx).run, argv, cwd, 30)
    if result.returncode != 0:
        raise ToolError(f"VS Code did not accept that: {result.output[-200:] or 'is the code command installed?'}", "vscode_failed")
    if args.action == "extensions":
        names = [x for x in result.output.splitlines() if x.strip()]
        return ToolResult.success(f"{len(names)} extension(s): " + ", ".join(names[:40]), extensions=names)
    return ToolResult.success({"goto": "Opened in VS Code.", "folder": "Folder opened in VS Code.", "diff": "Comparison opened in VS Code."}[args.action])


class OfficeArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=400, description="A .docx, .xlsx or .pptx file")


async def office_info(args: OfficeArgs, ctx: ToolContext) -> ToolResult:
    from app.core import office_info as O

    path = ctx.get_path_policy().resolve(args.path, must_exist=True)
    try:
        info = await asyncio.to_thread(O.summarize, path)
    except O.OfficeError as exc:
        raise ToolError(str(exc), "unreadable") from exc
    return ToolResult.success(O.render(info), **{k: v for k, v in info.items() if k in ("kind", "name")})


TOOLS: tuple[Tool, ...] = (
    Tool("vscode", "Control VS Code: open a file at a line, open a folder, compare two files, list installed extensions.", vscode, VSCodeArgs,
         RiskLevel.SAFE, "windows", timeout=40),
    Tool("office_info", "Describe a Word, Excel or PowerPoint file without opening Office: word count and headings, sheets and formulas, slide titles.",
         office_info, OfficeArgs, RiskLevel.SAFE, "windows", timeout=30),
)
