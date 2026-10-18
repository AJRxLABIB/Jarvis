"""Master prompt 3, stage A: approvals bound to the device's protected key, device-scoped standing approvals, and an
emergency stop that revokes grants at once and reports what it stopped and what it could not."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.remote import mobile


def test_approve_is_a_step_up_command_on_both_sides() -> None:
    assert "approve" in mobile.STEP_UP_COMMANDS
    java = (Path(__file__).resolve().parents[1] / "android/JarvisMobile/src/com/jarvis/mobile/Protocol.java").read_text()
    assert '"approve"' in java[java.index("STEP_UP"):]
    assert mobile._is_denial(json.dumps({"id": "q1", "approved": False}))
    assert not mobile._is_denial(json.dumps({"id": "q1", "approved": True}))
    assert not mobile._is_denial("not json") and not mobile._is_denial(json.dumps({"approved": "false"}))


def test_phone_requests_never_use_standing_approvals(tmp_path: Path) -> None:
    from app.agent.tool_manager import ToolExecutor
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.core import runs
    from app.memory.database import Database
    from app.security.grants import GrantStore
    from app.security.permissions import PermissionEngine
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry

    db = Database(tmp_path / "x.db")
    db.initialize()
    GrantStore(db).add("volume_control", minutes=30)
    executor = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()),
                            ToolContext(settings=Settings(data_dir=tmp_path), database=db))
    tool = executor.registry.get("volume_control")
    args = tool.args_model.model_validate({"action": "up"})
    assert executor._standing_grant(tool, args) is not None                 # on the PC: the grant applies
    with runs.running(runs.Run("phone", "volume up")):
        assert executor._standing_grant(tool, args) is None                 # from a phone: it asks


def test_emergency_stop_revokes_grants_and_itemises(tmp_path: Path) -> None:
    from app.core import emergency
    from app.memory.database import Database
    from app.security.grants import GrantStore
    from app.ui.controller_parts.proactive import ProactiveMixin

    db = Database(tmp_path / "x.db")
    db.initialize()
    GrantStore(db).add("volume_control", minutes=30)
    notices = []
    future = SimpleNamespace(done=lambda: False)
    fake = SimpleNamespace(
        ctx=SimpleNamespace(database=db), tts=SimpleNamespace(is_speaking=lambda: True), _listening=False,
        _pending_confirmation=SimpleNamespace(future=future, request=SimpleNamespace(summary="Delete report.docx")),
        _busy=True, _state="EXECUTING", cancel=lambda: None, notice=SimpleNamespace(emit=notices.append))
    fake._revoke_standing_approvals = lambda: ProactiveMixin._revoke_standing_approvals(fake)
    try:
        message = ProactiveMixin.emergency_stop(fake)
    finally:
        emergency.STATE.set("automation_disabled", False, by_voice=False)
    assert GrantStore(db).active() == []                                    # revoked at once, not lazily
    assert "speaking" in message and "Delete report.docx" in message and "1 standing approval" in message
    assert "Could not interrupt" in message and "not undone" in message


if __name__ == "__main__":
    pytest.main([__file__])


# ------------------------------------------------------------------ stage B: autonomy
def test_suggest_level_and_category_limits() -> None:
    from app.security import autonomy

    assert autonomy.LEVELS == ("observe", "suggest", "routine", "approval")
    msg = autonomy.blocked_in("suggest", "open_application", {"name": "Chrome"}, "apps")
    assert msg.startswith("Suggested, not done") and "open_application(name='Chrome')" in msg
    assert autonomy.blocked_in("suggest", "get_weather", {}, "info") is None             # reading still works
    blocked = autonomy.categories("files, Home ")
    assert autonomy.blocked_in("routine", "set_light", {}, "home", blocked) is not None
    assert autonomy.blocked_in("routine", "find_files", {}, "files", blocked) is None     # read-only stays allowed
    assert autonomy.must_ask("routine", "open_application", {}, "apps", autonomy.categories("apps"))
    assert not autonomy.must_ask("routine", "open_application", {}, "apps")
    assert autonomy.stricter("routine", "approval") == "approval" and autonomy.stricter("observe", "routine") == "observe"
    assert autonomy.stricter("suggest", "approval") == "suggest"


def test_executor_applies_suggest_and_the_remote_level(tmp_path: Path) -> None:
    import asyncio

    from app.agent.tool_manager import ToolExecutor
    from app.ai.base import ToolCall
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.core import runs
    from app.security.permissions import PermissionEngine
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry

    def executor(**settings):
        return ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()),
                            ToolContext(settings=Settings(data_dir=tmp_path, **settings)))

    call = ToolCall("volume_control", {"action": "up"})
    result = asyncio.run(executor(autonomy="suggest").execute(call)).result
    assert not result.ok and result.error == "suggest_mode" and "volume_control" in result.message
    remote = executor(autonomy="routine", autonomy_remote="observe")
    with runs.running(runs.Run("phone", "volume up")):
        assert asyncio.run(remote.execute(call)).result.error == "observe_mode"         # a phone request: stricter
    blocked = executor(autonomy_block_categories="windows")
    assert asyncio.run(blocked.execute(call)).result.error == "observe_mode"


# ------------------------------------------------------------------ stage C: no silent model switching
def test_model_substitution_is_announced_once() -> None:
    from app.ai.manager import AIManager

    manager = AIManager.__new__(AIManager)
    assert manager._announce_once("x can't use tools") == "x can't use tools"
    assert manager._announce_once("x can't use tools") is None             # told once, then only logged
    assert manager._announce_once("y is too big for this PC's memory")


# ------------------------------------------------------------------ stage G: the phone can cancel the current request
def test_phone_cancel_command(tmp_path: Path) -> None:
    import asyncio

    from app.remote.mobile import COMMANDS
    from app.remote.server import RemoteServer

    assert COMMANDS["cancel"][0] == "chat" and "cancel" not in mobile.STEP_UP_COMMANDS     # stopping is always safe
    calls = []
    hub = SimpleNamespace(cancel=lambda: calls.append(1) or "Cancelled the current request.")
    server = SimpleNamespace(hub=hub)
    device = SimpleNamespace(id="p1", name="Pixel", trust="trusted", permissions=["chat"])
    out = asyncio.run(RemoteServer._mobile_run(server, device, "cancel", {}))
    assert out == {"ok": True, "message": "Cancelled the current request."} and calls == [1]


def test_capability_matrix_is_generated_from_the_registry_and_docs_are_in_sync() -> None:
    from app.core.capabilities import FEATURES, markdown
    from app.tools.catalog import build_default_registry

    registry = build_default_registry()
    text = markdown(registry)
    assert f"## Tools ({len(registry.tools)})" in text and "| get_weather | " in text
    assert all(a in ("offline", "offline after setup", "local network", "internet", "credentials",
                     "hardware / OS permission") for _, a, _ in FEATURES)
    doc = (Path(__file__).resolve().parents[1] / "docs" / "CAPABILITIES.md").read_text(encoding="utf-8")
    assert doc == text, "docs/CAPABILITIES.md is stale: regenerate it with python run.py --capabilities"
