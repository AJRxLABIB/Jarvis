"""Standing approvals ("don't ask me again for a while") wired into the tool executor. The rules themselves live in
app/security/grants.py and were never exercised before: they are tested here, end to end."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agent.tool_manager import ToolExecutor, ToolRegistry
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import emergency
from app.memory.audit import AuditLog
from app.memory.database import Database
from app.security import grants
from app.security.confirmations import ConfirmationRequest, Confirmer
from app.security.grants import GrantStore, grantable
from app.security.permissions import PermissionEngine, RiskLevel
from app.tools.approvals_tool import ApprovalsArgs, _check, _question, _risk, approvals
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.tools.catalog import build_default_registry


class Ask(Confirmer):
    def __init__(self, answer: bool = True) -> None:
        self.answer, self.asked = answer, 0

    async def confirm(self, request: ConfirmationRequest) -> bool:
        self.asked += 1
        return self.answer


class VolArgs(ToolArgs):
    action: str = "up"


@pytest.fixture()
def env(tmp_path: Path):
    db = Database(tmp_path / "j.db")
    db.initialize()
    ran: list[str] = []
    registry = ToolRegistry([
        Tool("volume_control", "change volume", lambda a, c: ran.append(a.action) or ToolResult.success("ok"), VolArgs,
             RiskLevel.MODERATE, "system"),
        Tool("delete_item", "delete", lambda a, c: ToolResult.success("gone"), NoArgs, RiskLevel.MODERATE, "files"),
        Tool("send_note", "sends out", lambda a, c: ToolResult.success("sent"), NoArgs, RiskLevel.MODERATE, "messages"),
    ])
    confirmer = Ask()
    ctx = ToolContext(Settings(), database=db, clock=lambda: datetime.now(timezone.utc))
    executor = ToolExecutor(registry, PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer, audit=AuditLog(db))
    emergency.STATE.flags = {k: False for k in emergency.STATE.flags}
    yield db, executor, confirmer, ran, ctx
    emergency.STATE.flags = {k: False for k in emergency.STATE.flags}


def run(executor, name="volume_control", **args):
    return asyncio.run(executor.execute(ToolCall(name, args)))


def test_without_a_grant_a_moderate_action_asks_every_time(env) -> None:
    _, executor, confirmer, ran, _ = env
    run(executor)
    run(executor)
    assert confirmer.asked == 2 and ran == ["up", "up"]


def test_a_grant_skips_the_question_counts_uses_and_is_audited(env) -> None:
    db, executor, confirmer, ran, _ = env
    grant = GrantStore(db).add("volume_control", minutes=30)
    assert run(executor).result.ok and run(executor, action="down").result.ok
    assert confirmer.asked == 0 and ran == ["up", "down"] and GrantStore(db).get(grant.id).uses == 2
    events = [r["decision"] for r in AuditLog(db).recent(30) if r["event"] == "confirmation_skipped"]
    assert events.count("standing_approval") == 2


def test_a_grant_can_be_narrowed_to_exact_argument_values(env) -> None:
    db, executor, confirmer, _, _ = env
    GrantStore(db).add("volume_control", minutes=30, match={"action": "up"})
    run(executor, action="up")
    assert confirmer.asked == 0
    run(executor, action="down")                       # a different value: asks again
    assert confirmer.asked == 1


def test_expired_and_revoked_grants_do_nothing(env) -> None:
    db, executor, confirmer, _, _ = env
    store = GrantStore(db)
    old = store.add("volume_control", minutes=1)
    with db.transaction() as conn:
        conn.execute("UPDATE approval_grants SET expires_at = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds"), old.id))
    run(executor)
    assert confirmer.asked == 1
    fresh = store.add("volume_control", minutes=30)
    assert store.revoke(fresh.id) and not store.revoke(fresh.id)
    run(executor)
    assert confirmer.asked == 2


def test_an_emergency_switch_ignores_and_revokes_every_grant(env) -> None:
    db, executor, confirmer, _, _ = env
    store = GrantStore(db)
    store.add("volume_control", minutes=30)
    emergency.STATE.flags["automation_disabled"] = True
    run(executor)
    assert confirmer.asked == 1 and store.active() == []


def test_a_grant_never_covers_things_that_send_out_even_if_one_were_stored(env) -> None:
    """grantable() is re-checked when a grant is used: a row put in the database by other means still cannot skip a question."""
    db, executor, confirmer, _, _ = env
    executor.registry.register(Tool("mcp_post", "posts online", lambda a, c: ToolResult.success("posted"), NoArgs,
                                    RiskLevel.MODERATE, "mcp_server"))
    GrantStore(db).add("mcp_post", minutes=30)
    run(executor, "mcp_post")
    assert confirmer.asked == 1


@pytest.mark.parametrize("tool, category, level, ok", [
    ("volume_control", "system", RiskLevel.MODERATE, True),
    ("delete_item", "files", RiskLevel.MODERATE, False), ("power_control", "system", RiskLevel.MODERATE, False),
    ("send_note", "mcp_x", RiskLevel.MODERATE, False), ("volume_control", "system", RiskLevel.DANGEROUS, False),
    ("volume_control", "system", RiskLevel.SAFE, False),
])
def test_grantable_rules(tool, category, level, ok) -> None:
    assert grantable(tool, category, level)[0] is ok


def test_session_grants_end_with_the_session_and_minutes_are_capped(env) -> None:
    db, *_ = env
    store = GrantStore(db)
    huge = store.add("volume_control", minutes=10_000)
    assert (datetime.fromisoformat(huge.expires_at) - datetime.fromisoformat(huge.created_at)) <= timedelta(minutes=grants.MAX_MINUTES)
    session = store.add("volume_control", minutes=None, match={"action": "up"})
    assert session.scope == "session" and session.active and session.covers("volume_control", {"action": "up"})
    assert not session.covers("volume_control", {"action": "down"}) and not session.covers("other", {"action": "up"})
    assert "until Jarvis restarts" in session.describe()
    assert store.revoke_all() == 2 and store.active() == []


# ------------------------------------------------------------------------------- the tool


def test_the_tool_asks_for_confirmation_to_create_but_not_to_list_or_end(env) -> None:
    _, _, _, _, ctx = env
    assert _risk(ApprovalsArgs(action="allow", tool="volume_control"), ctx) == RiskLevel.DANGEROUS
    for action in ("list", "revoke", "revoke_all"):
        assert _risk(ApprovalsArgs(action=action), ctx) == RiskLevel.SAFE
    assert "30 minutes" in _question(ApprovalsArgs(action="allow", tool="volume_control"), ctx)
    assert "until Jarvis restarts" in _question(ApprovalsArgs(action="allow", tool="volume_control", minutes=None), ctx)


def test_the_tool_refuses_what_can_never_be_granted() -> None:
    registry = build_default_registry()
    ctx = ToolContext(Settings())
    ctx.get_tool_registry = lambda: registry
    for name in ("delete_item", "power_control", "run_command", "volume_control"):
        with pytest.raises(ToolError) as exc:
            _check(ApprovalsArgs(action="allow", tool=name), ctx)
        assert exc.value.code == "not_grantable"
    with pytest.raises(ToolError) as exc:
        _check(ApprovalsArgs(action="allow", tool="no_such_tool"), ctx)
    assert exc.value.code == "unknown_tool"
    with pytest.raises(ToolError):
        _check(ApprovalsArgs(action="allow", tool="copy_to_clipboard", only_when={"a": [1]}), ctx)
    _check(ApprovalsArgs(action="allow", tool="copy_to_clipboard"), ctx)       # a moderate, harmless tool is fine


def test_the_tool_lists_creates_and_ends_approvals(env) -> None:
    db, _, _, _, ctx = env
    registry = build_default_registry()
    ctx.get_tool_registry = lambda: registry
    assert "No standing approvals" in approvals(ApprovalsArgs(), ctx).message
    made = approvals(ApprovalsArgs(action="allow", tool="copy_to_clipboard", minutes=15, only_when={"text": "hi"}), ctx)
    assert made.ok and "won't ask before 'copy_to_clipboard'" in made.message
    listed = approvals(ApprovalsArgs(), ctx)
    assert "copy_to_clipboard" in listed.message and listed.data["grants"][0]["match"] == {"text": "hi"}
    with pytest.raises(ToolError):
        approvals(ApprovalsArgs(action="revoke"), ctx)
    with pytest.raises(ToolError):
        approvals(ApprovalsArgs(action="revoke", grant_id=999), ctx)
    assert approvals(ApprovalsArgs(action="revoke", grant_id=made.data["grant"]), ctx).ok
    approvals(ApprovalsArgs(action="allow", tool="copy_to_clipboard"), ctx)
    assert "Ended 1" in approvals(ApprovalsArgs(action="revoke_all"), ctx).message
