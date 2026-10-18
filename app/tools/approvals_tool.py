"""approvals tool: standing "don't ask me again for a while" approvals (rules in app/security/grants.py).

Creating one is a DANGEROUS action: the window (or your phone's fingerprint) always asks first, whoever suggested it. The
rules are fixed in code: moderate actions only, never anything that sends, saves, deletes or changes settings, at most
12 hours, optionally narrowed to exact argument values, ignored while untrusted text steers a request or an emergency
switch is on, and revoked by an emergency stop.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.security.grants import MAX_MINUTES, GrantStore, grantable
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class ApprovalsArgs(ToolArgs):
    action: Literal["list", "allow", "revoke", "revoke_all"] = Field(
        default="list", description="list: active standing approvals; allow: stop asking about one tool for a while "
                                    "(you confirm first); revoke: end one; revoke_all: end all")
    tool: str = Field(default="", max_length=60, description="The tool to stop asking about (allow), e.g. 'volume_control'")
    minutes: int | None = Field(default=30, ge=1, le=MAX_MINUTES, description="How long (1-720 minutes); empty = until Jarvis restarts")
    only_when: dict[str, Any] = Field(default_factory=dict, description="Optional exact argument values the approval is limited "
                                                                       "to, e.g. {\"action\": \"up\"}")
    grant_id: int = Field(default=0, ge=0, description="The approval number from the list (revoke)")


def _risk(args: ApprovalsArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.DANGEROUS if args.action == "allow" else RiskLevel.SAFE


def _question(args: ApprovalsArgs, _ctx: ToolContext) -> str:
    span = "until Jarvis restarts" if args.minutes is None else f"for {args.minutes} minutes"
    narrow = f" (only when {', '.join(f'{k}={v!r}' for k, v in args.only_when.items())})" if args.only_when else ""
    return (f"Stop asking before every '{args.tool}'{narrow} {span}? Jarvis will run it without asking until then. "
            "You can end this any time, and an emergency stop cancels it.")


def _check(args: ApprovalsArgs, ctx: ToolContext) -> None:
    if args.action != "allow":
        return
    tool = ctx.get_tool_registry().get(args.tool.strip())
    if tool is None:
        raise ToolError(f"There is no tool called '{args.tool}'.", "unknown_tool")
    ok, why = grantable(tool.name, tool.category, RiskLevel.MODERATE if tool.level == RiskLevel.MODERATE else tool.level)
    if not ok:
        raise ToolError(f"I can't do that: {why}.", "not_grantable")
    if len(args.only_when) > 5 or any(not isinstance(v, (str, int, float, bool)) for v in args.only_when.values()):
        raise ToolError("Limit it with at most 5 simple values.", "bad_match")


def approvals(args: ApprovalsArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("I have no database here.", "unavailable")
    store = GrantStore(ctx.database)
    if args.action == "list":
        active = store.active()
        if not active:
            return ToolResult.success("No standing approvals: I ask before every moderate action.", grants=[])
        return ToolResult.success("Standing approvals:\n" + "\n".join(g.describe() for g in active),
                                  grants=[{"id": g.id, "tool": g.tool, "match": g.match, "uses": g.uses} for g in active])
    if args.action == "revoke_all":
        return ToolResult.success(f"Ended {store.revoke_all()} standing approval(s). I ask every time again.")
    if args.action == "revoke":
        if not args.grant_id:
            raise ToolError("Which approval? Give its number from the list.", "missing_id")
        if not store.revoke(args.grant_id):
            raise ToolError("There is no such active approval.", "unknown_grant")
        return ToolResult.success("Ended that approval.")
    _check(args, ctx)
    tool = args.tool.strip()
    grant = store.add(tool, minutes=args.minutes, match=args.only_when, note="created by you in a confirmed request")
    return ToolResult.success(f"Okay. I won't ask before '{tool}' {grant.describe().split(' - ', 1)[1]}.", grant=grant.id)


TOOLS = (
    Tool("approvals", "Standing approvals: list them, stop asking before a harmless-but-moderate action for a while ('don't ask me "
         "again about volume for 30 minutes'), or end them. Creating one always asks the user first.",
         approvals, ApprovalsArgs, RiskLevel.SAFE, "approvals", precheck=_check, risk=_risk, confirmation=_question),
)
