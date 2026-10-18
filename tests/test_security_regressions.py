"""Security regressions: things an attacker (or a confused model) might try. Passing these does not prove the system
is secure; they stop known mistakes from coming back."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.security.validator import PathPolicy, Zone
from app.tools.base import ToolError
from app.utils.redaction import redact
from tests.test_v3230 import make


def policy(tmp_path: Path) -> PathPolicy:
    safe, secret = tmp_path / "safe", tmp_path / "secret"
    safe.mkdir()
    secret.mkdir()
    (secret / "keys.txt").write_text("x")
    return PathPolicy(known={"HOME": tmp_path}, safe_roots=[Path(os.path.realpath(safe))],
                      protected_roots=[Path(os.path.realpath(secret))])


@pytest.mark.parametrize("text", ["..", "../x", "safe/../secret/keys.txt", "safe\\..\\..\\etc", "a/./b/../../..", "CON",
                                  "safe/bad|name", "\\\\server\\share\\x", "//server/share/x"])
def test_path_tricks_are_refused_or_stay_inside(tmp_path: Path, text: str) -> None:
    pol = policy(tmp_path)
    try:
        resolved = pol.resolve(text, must_exist=False)
    except ToolError:
        return  # refused outright
    assert pol.zone(resolved) != Zone.SAFE or str(resolved).startswith(str(tmp_path / "safe"))


@pytest.mark.skipif(os.name == "nt", reason="symlinks need rights on Windows")
def test_a_link_inside_the_safe_zone_cannot_reach_a_protected_folder(tmp_path: Path) -> None:
    pol = policy(tmp_path)
    (tmp_path / "safe" / "shortcut").symlink_to(tmp_path / "secret", target_is_directory=True)
    resolved = pol.resolve(str(tmp_path / "safe" / "shortcut" / "keys.txt"), must_exist=True)
    assert pol.zone(resolved) == Zone.PROTECTED  # links are followed before the decision


def test_an_agent_cannot_use_a_tool_outside_its_contract_even_if_the_plan_asks() -> None:
    from app.core import orchestrator

    ex, log = make()
    steps = orchestrator.validate_plan([{"agent": "researcher", "tool": "web_search", "arguments": {"name": "a"}}],
                                       ex.registry)
    steps[0].tool = "delete_item"  # escalate after validation
    outcome = asyncio.run(orchestrator.run_plan(ex, "g", steps, approved=True))
    assert not outcome.ok and log == [] and steps[0].state == "failed"


def test_approval_does_not_unlock_a_tool_the_contract_denies() -> None:
    from app.core.agents import contract_for

    ok, why = contract_for("secretary").allows("send_email", "office", __import__("app.security.permissions",
                                                                                   fromlist=["RiskLevel"]).RiskLevel.MODERATE)
    assert not ok and "not allowed" in why


def test_critical_actions_stay_blocked_for_everyone() -> None:
    from app.security.permissions import Decision, PermissionEngine, RiskLevel

    decision, _ = PermissionEngine(PermissionsConfig()).decide("anything", RiskLevel.CRITICAL)
    assert decision != Decision.ALLOW


@pytest.mark.parametrize("line", ["API_TOKEN=abcd1234efgh5678", "password: hunter2hunter2", "Authorization: Bearer abcdefghijklmnop1234",
                                  "DB_PASSWORD=supersecretvalue1"])
def test_secrets_are_redacted_from_text(line: str) -> None:
    out = redact(line)
    assert "abcd1234efgh5678" not in out and "hunter2hunter2" not in out and "supersecretvalue1" not in out
    assert "abcdefghijklmnop1234" not in out


def test_unknown_tool_names_from_a_model_never_execute() -> None:
    ex, log = make()
    result = asyncio.run(ex.execute(ToolCall("run_anything", {"name": "rm -rf /"}))).result
    assert not result.ok and result.error == "unknown_tool" and log == []
