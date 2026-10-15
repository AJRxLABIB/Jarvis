"""3.23: the agent orchestrator - role-scoped agent contracts enforced by the same executor, and running a
validated multi-agent plan. Adversarial cases: a plan can't widen what an agent may do."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import Field

from app.agent.tool_manager import ToolExecutor, ToolRegistry
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import orchestrator
from app.core.agents import AGENTS, KNOWN_CATEGORIES, contract_for
from app.security.permissions import PermissionEngine, RiskLevel
from app.tools import agents_tools
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from tests.test_tools import ScriptedConfirmer


class NameArgs(ToolArgs):
    name: str = Field(default="x", max_length=50)


def make(confirm=True):
    log: list[str] = []

    def handler(cat):
        def run(args, ctx):
            log.append(f"{cat}:{args.name}")
            return ToolResult.success(f"{cat} did {args.name}")
        return run

    tools = [
        Tool("web_search", "search", handler("web"), NameArgs, RiskLevel.SAFE, "web"),
        Tool("browser_read", "read", handler("browser"), NameArgs, RiskLevel.SAFE, "browser"),
        Tool("move_item", "move", handler("files"), NameArgs, RiskLevel.MODERATE, "files"),
        Tool("delete_item", "delete", handler("files"), NameArgs, RiskLevel.DANGEROUS, "files"),
        Tool("edit_code", "edit", handler("dev"), NameArgs, RiskLevel.MODERATE, "dev"),
        Tool("run_command", "run", handler("dev"), NameArgs, RiskLevel.DANGEROUS, "dev"),
        Tool("send_email", "send", handler("office"), NameArgs, RiskLevel.MODERATE, "office"),
        *agents_tools.TOOLS,
    ]
    ctx = ToolContext(Settings())
    ex = ToolExecutor(ToolRegistry(tools), PermissionEngine(PermissionsConfig(confirm_moderate=True)), ctx,
                      confirmer=ScriptedConfirmer(confirm))
    return ex, log


def go(ex, tool, **arguments):
    return asyncio.run(ex.execute(ToolCall(tool, arguments))).result


# ------------------------------------------------------------------ contracts


def test_every_agent_uses_known_categories() -> None:
    for agent in AGENTS.values():
        assert agent.categories <= KNOWN_CATEGORIES, (agent.name, agent.categories - KNOWN_CATEGORIES)


def test_contract_allows_within_scope_and_risk() -> None:
    researcher = contract_for("researcher")
    assert researcher.allows("web_search", "web", RiskLevel.SAFE)[0]
    # out of category
    assert not researcher.allows("move_item", "files", RiskLevel.MODERATE)[0]
    # read-only agent refuses anything above SAFE even in an allowed-looking category
    assert not researcher.allows("remember", "memory", RiskLevel.SAFE)[0]  # explicitly denied
    organizer = contract_for("organizer")
    assert organizer.allows("move_item", "files", RiskLevel.MODERATE)[0]
    assert not organizer.allows("delete_item", "files", RiskLevel.DANGEROUS)[0]  # denied + above risk


# ------------------------------------------------------------------ executor enforcement


def test_executor_refuses_tools_outside_the_active_contract() -> None:
    ex, log = make()
    ex.context.agent_contract = contract_for("researcher")
    try:
        allowed = go(ex, "web_search", name="quantum")
        blocked = go(ex, "move_item", name="a")
    finally:
        ex.context.agent_contract = None
    assert allowed.ok and log == ["web:quantum"]
    assert not blocked.ok and blocked.error == "agent_not_allowed"


def test_contract_caps_risk_even_if_permissions_would_allow() -> None:
    ex, log = make()
    ex.context.agent_contract = contract_for("coder")  # max MODERATE, dev+files
    try:
        edit = go(ex, "edit_code", name="main.py")     # MODERATE, allowed
        run = go(ex, "run_command", name="rm -rf")     # DANGEROUS + denied
    finally:
        ex.context.agent_contract = None
    assert edit.ok
    assert not run.ok and run.error == "agent_not_allowed" and log == ["dev:main.py"]


# ------------------------------------------------------------------ plan validation


def test_plan_validation_rejects_out_of_scope_steps() -> None:
    ex, _ = make()
    reg = ex.registry
    with pytest.raises(ToolError, match="no 'wizard' agent"):
        orchestrator.validate_plan([{"agent": "wizard", "tool": "web_search"}], reg)
    with pytest.raises(ToolError, match="researcher"):
        orchestrator.validate_plan([{"agent": "researcher", "tool": "delete_item"}], reg)
    good = orchestrator.validate_plan([{"agent": "researcher", "tool": "web_search", "arguments": {"name": "x"}}], reg)
    assert good[0].agent == "researcher" and good[0].tool == "web_search"


# ------------------------------------------------------------------ running a plan


def test_run_team_runs_each_step_under_its_agent(monkeypatch) -> None:
    ex, log = make()
    result = go(ex, "run_team", goal="research and tidy", steps=[
        {"agent": "researcher", "tool": "web_search", "arguments": {"name": "topic"}, "title": "look it up"},
        {"agent": "organizer", "tool": "move_item", "arguments": {"name": "report"}, "title": "file it"},
    ])
    assert result.ok, result.message
    assert log == ["web:topic", "files:report"]
    assert ex.context.agent_contract is None  # restored after the run
    assert result.data["steps"][0]["state"] == "done"


def test_run_team_refuses_a_plan_that_oversteps_an_agent() -> None:
    ex, log = make()
    # ask the researcher (read-only) to delete - validation refuses before anything runs
    result = go(ex, "run_team", goal="sneaky", steps=[
        {"agent": "researcher", "tool": "delete_item", "arguments": {"name": "x"}},
    ])
    assert not result.ok and log == []


def test_delegate_one_agent() -> None:
    ex, log = make()
    result = go(ex, "delegate", agent="organizer", steps=[
        {"tool": "move_item", "arguments": {"name": "a"}},
        {"tool": "move_item", "arguments": {"name": "b"}},
    ])
    assert result.ok and log == ["files:a", "files:b"]


def test_delegate_saying_no_to_the_plan_does_nothing() -> None:
    ex, log = make(confirm=False)
    # organizer move_item is MODERATE -> the plan asks once; saying no cancels
    result = go(ex, "delegate", agent="organizer", steps=[{"tool": "move_item", "arguments": {"name": "a"}}])
    assert not result.ok and result.error == "declined" and log == []


def test_secretary_cannot_send_email_even_if_asked() -> None:
    ex, log = make()
    result = go(ex, "delegate", agent="secretary", steps=[{"tool": "send_email", "arguments": {"name": "boss"}}])
    assert not result.ok and log == []  # send_email is denied for the secretary


def test_list_agents() -> None:
    ex, _ = make()
    result = go(ex, "list_agents")
    assert result.ok and {a["name"] for a in result.data["agents"]} == set(AGENTS)


@pytest.mark.parametrize("said,tool", [
    ("list agents", "list_agents"),
    ("who's on my team", "list_agents"),
])
def test_agent_commands(said, tool) -> None:
    from app.agent.shortcuts import match_shortcut

    call = match_shortcut(said, lambda n: None)
    assert call is not None and call.name == tool
