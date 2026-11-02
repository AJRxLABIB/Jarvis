"""skill_card: an honest overview of a saved routine - needs, risks, failure handling, undo, history (app/automation/skill_card.py)."""

from __future__ import annotations

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class SkillCardArgs(ToolArgs):
    name: str = Field(default="", max_length=80, description="The saved routine / workflow name (empty = list them all)")


def skill_card(args: SkillCardArgs, ctx: ToolContext) -> ToolResult:
    from app.automation import skill_card as S
    from app.automation.tasks import AutomationStore

    store = ctx.get_automation_store()
    if store is None:
        raise ToolError("Saved routines need the database.", "no_database")
    tasks = store.all()
    if not args.name.strip():
        if not tasks:
            return ToolResult.success("No saved routines yet.", skills=[])
        return ToolResult.success("\n".join(f"{t.name}: {t.trigger.describe()} ({len(t.steps)} step(s))" + ("" if t.enabled else " (paused)") for t in tasks),
                                  skills=[t.name for t in tasks])
    key = args.name.lower().strip()
    hit = [t for t in tasks if t.name.lower() == key] or [t for t in tasks if key in t.name.lower()]
    if len(hit) != 1:
        raise ToolError(f"Which one? {', '.join(t.name for t in tasks) or 'none saved'}.", "ambiguous" if hit else "not_found")
    card = S.build(hit[0], ctx.get_tool_registry(), ctx.database)
    return ToolResult.success(card.render(), **card.to_dict())


TOOLS: tuple[Tool, ...] = (
    Tool("skill_card", "Show one saved routine / workflow as a card: its inputs, the tools, apps and abilities it needs, the risk of each step, what happens "
         "if a step fails, which steps can be undone, how often it worked, and what is NOT recorded about it. With no name, lists the saved routines. "
         "For 'show my routine', 'what does this workflow do', 'is this skill safe'.", skill_card, SkillCardArgs, RiskLevel.SAFE, "automation"),
)
