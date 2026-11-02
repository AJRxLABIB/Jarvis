"""Make a new reusable tool from a request (rules in app/core/foundry.py). It is a parameterised workflow of EXISTING tools -
never generated code. 'plan' drafts, validates, previews and tests it with mock tools and saves nothing; 'save' does the same and then
saves it through the normal workflow path (which asks you on screen)."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class FoundryArgs(ToolArgs):
    action: Literal["plan", "save"] = Field(
        default="plan",
        description="plan = draft, validate, preview (risk, paths, undo) and test with mock tools; nothing is saved. save = the same, then "
                    "save it as a reusable workflow you can run with named inputs (asks you first)")
    request: str = Field(default="", max_length=1500, description="What the new tool should do, in plain words (the local model drafts it)")
    spec: str = Field(default="", max_length=6000,
                      description='Or give the design yourself as JSON: {"name", "description", "inputs": {...}, "steps": [{"tool", "arguments"}]}')


async def foundry_tool(args: FoundryArgs, ctx: ToolContext) -> ToolResult:
    from app.core import foundry
    from app.tools.automation import ManualTrigger, _save

    if not args.request.strip() and not args.spec.strip():
        raise ToolError("Say what the tool should do (request) or give a spec.", "missing_request")
    draft = foundry.parse_draft(args.spec) if args.spec.strip() else await foundry.draft_from_request(args.request, ctx)
    report = foundry.Report()
    if foundry.check(draft, ctx, report) and foundry.preview(draft, ctx, report):
        await foundry.test_mock(draft, ctx, report)
    if args.action == "save":
        if not report.ok:
            return ToolResult.failure("not_ready", report.describe(), saved=False, stages=[s for s, _, _ in report.stages])
        from app.automation.tasks import parse_steps

        _save(ctx, draft.name, ManualTrigger(), parse_steps(draft.steps), draft.inputs or None)
        report.saved = True
    return ToolResult.success(report.describe(), saved=report.saved, ready=report.ok, simulated=True,
                              name=draft.name, inputs=sorted(draft.inputs))


def _risk(args: FoundryArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "save" else RiskLevel.SAFE


def _question(args: FoundryArgs, _ctx: ToolContext) -> str:
    try:
        name = json.loads(args.spec).get("name", "") if args.spec.strip() else ""
    except (ValueError, AttributeError):
        name = ""
    return (f"Save the new tool '{name or 'drafted from your request'}' as a reusable workflow? "
            "It only uses existing tools, which still ask for their own permissions when they run.")


TOOLS: tuple[Tool, ...] = (
    Tool("build_tool", "Create a NEW reusable tool from a request: a workflow of existing tools with named inputs. It is drafted, validated, "
         "previewed (risk, paths, undo) and tested with mock tools before anything is saved; it never writes or runs code and cannot "
         "grant itself permissions. Use plan to review, save to keep it.",
         foundry_tool, FoundryArgs, RiskLevel.SAFE, "automation", timeout=120, risk=_risk, confirmation=_question),
)
