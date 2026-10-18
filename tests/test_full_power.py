"""FULL_POWER: nothing is off-limits, but every normally-refused (CRITICAL) action asks the user on screen - never by
voice, never from a phone, never in an automation, never pre-approved. The secrets file is never changed."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai.base import ToolCall
from app.security.permissions import RiskLevel


def _executor(tmp_path: Path, full_power: bool, answers: list[bool]):
    from app.agent.tool_manager import ToolExecutor, ToolRegistry
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine
    from app.tools.base import NoArgs, Tool, ToolContext, ToolResult

    ran, asked = [], []

    class Confirmer:
        async def confirm(self, request):
            asked.append((request.level, request.summary))
            return answers.pop(0) if answers else False

    registry = ToolRegistry()
    registry.register(Tool("format_disk", "Format a disk", lambda a, c: (ran.append(1), ToolResult.success("ok"))[1],
                           NoArgs, RiskLevel.CRITICAL, "system", confirmation=lambda a, c: "Format drive E:?"))
    executor = ToolExecutor(registry, PermissionEngine(PermissionsConfig()),
                            ToolContext(settings=Settings(data_dir=tmp_path, full_power=full_power)),
                            confirmer=Confirmer())
    return executor, ran, asked


def test_without_full_power_critical_is_refused(tmp_path: Path) -> None:
    executor, ran, asked = _executor(tmp_path, False, [True])
    result = asyncio.run(executor.execute(ToolCall("format_disk", {}), user_command=True)).result
    assert not result.ok and result.error == "blocked" and "FULL_POWER is off" in result.message
    assert ran == [] and asked == []


def test_full_power_asks_and_only_your_yes_runs_it(tmp_path: Path) -> None:
    executor, ran, asked = _executor(tmp_path, True, [False, True])
    no = asyncio.run(executor.execute(ToolCall("format_disk", {}), user_command=True)).result
    assert not no.ok and ran == []
    assert asked[0][0] == RiskLevel.CRITICAL and asked[0][1].startswith("CRITICAL")
    yes = asyncio.run(executor.execute(ToolCall("format_disk", {}), user_command=True)).result
    assert yes.ok and ran == [1]                                             # asked again: no reuse of a yes


@pytest.mark.parametrize("source", ["phone", "automation", "internal"])
def test_full_power_never_applies_to_phones_or_automations(tmp_path: Path, source) -> None:
    from app.core import runs

    executor, ran, asked = _executor(tmp_path, True, [True])
    with runs.running(runs.Run(source, "format")):
        result = asyncio.run(executor.execute(ToolCall("format_disk", {}))).result
    assert result.error == "blocked" and ran == [] and asked == []


def test_critical_is_never_answered_by_voice_or_phone() -> None:
    from app.ui.controller_parts.voice import VoiceMixin

    spoken = []
    fake = SimpleNamespace(hands_free=object(), ctx=SimpleNamespace(settings=SimpleNamespace(voice_confirm=True)),
                           tts=SimpleNamespace(speak=spoken.append))
    pending = SimpleNamespace(request=SimpleNamespace(level=RiskLevel.CRITICAL, summary="Format?"), heard="")
    VoiceMixin.ask_by_voice(fake, pending)
    assert spoken == ["This one is critical. Please answer on the screen."]
    source = (Path(__file__).resolve().parents[1] / "app/ui/controller_parts/remote.py").read_text()
    assert "remote_critical_refused" in source


def test_security_core_proposals_only_in_full_power_and_never_secrets() -> None:
    from app.evolution.proposals import check_allowed
    from app.tools.base import ToolError

    with pytest.raises(ToolError):
        check_allowed("app/security/permissions.py")
    check_allowed("app/security/permissions.py", full_power=True)
    for secret in (".env", ".env.bak"):
        with pytest.raises(ToolError):
            check_allowed(secret, full_power=True)
