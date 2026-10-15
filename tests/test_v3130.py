"""3.13.0 Phase 10: skill (workflow) versions and trusted plugin manifests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.memory.database import Database
from app.plugins import load_plugins, manifest_path, plugin_status, trust_plugin
from app.security.permissions import PermissionEngine
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry
from tests.test_tools import ScriptedConfirmer


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    database = Database(tmp_path / "j.db")
    database.initialize()
    return ToolContext(Settings(), database=database)


def run(ctx, tool, confirm=True, **arguments):
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx,
                      confirmer=ScriptedConfirmer(confirm))
    ctx.executor = ex
    return asyncio.run(ex.execute(ToolCall(tool, arguments))).result


def test_changed_workflows_keep_their_old_versions(ctx) -> None:
    first = run(ctx, "save_workflow", name="study", steps=[{"tool": "open_website", "arguments": {"site": "youtube"}}])
    assert first.ok
    run(ctx, "save_workflow", name="study", steps=[{"tool": "get_datetime"}, {"tool": "open_website",
                                                                             "arguments": {"site": "gmail"}}])
    versions = run(ctx, "workflow_versions", name="study")
    assert versions.ok and versions.data["versions"][0]["version"] == 1 and versions.data["versions"][0]["steps"] == 1
    restored = run(ctx, "restore_workflow", name="study", version=1)
    assert restored.ok and len(restored.data["steps"]) == 1
    # the two-step version was kept too
    assert [v["version"] for v in run(ctx, "workflow_versions", name="study").data["versions"]] == [2, 1]


def test_deleted_workflows_can_come_back(ctx) -> None:
    run(ctx, "save_workflow", name="focus", steps=[{"tool": "get_datetime"}])
    assert run(ctx, "manage_automation", name="focus", action="delete").ok
    assert run(ctx, "workflow_versions", name="focus").data["versions"][0]["reason"] == "deleted"
    assert run(ctx, "restore_workflow", name="focus", version=1).ok


def test_restore_asks_first(ctx) -> None:
    run(ctx, "save_workflow", name="x1", steps=[{"tool": "get_datetime"}])
    run(ctx, "save_workflow", name="x1", steps=[{"tool": "get_datetime"}, {"tool": "get_datetime"}])
    assert not run(ctx, "restore_workflow", confirm=False, name="x1", version=1).ok


PLUGIN = ("from app.tools.base import Tool, NoArgs, ToolResult\nVERSION = '2.1'\n"
          "TOOLS = [Tool('say_hello', 'Say hello', lambda a, c: ToolResult.success('Hello!'), NoArgs)]\n")


def test_untrusted_plugins_never_run(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("raise SystemExit('I ran!')\n")
    report = load_plugins(tmp_path, build_default_registry())
    assert report.loaded == () and "not trusted" in report.failed[0][1]
    assert plugin_status(tmp_path)[0]["status"] == "untrusted"


def test_trusted_plugin_loads_and_a_changed_one_does_not(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text(PLUGIN)
    manifest = trust_plugin(tmp_path, "hello")
    assert manifest["version"] == "2.1" and manifest["tools"] == {"say_hello": 0}
    registry = build_default_registry()
    assert load_plugins(tmp_path, registry).loaded == ("hello",) and "say_hello" in registry
    (tmp_path / "hello.py").write_text(PLUGIN + "raise SystemExit('tampered code ran')\n")
    report = load_plugins(tmp_path, build_default_registry())
    assert report.loaded == () and "changed since you trusted it" in report.failed[0][1]
    assert plugin_status(tmp_path)[0]["status"] == "changed"


def test_plugins_cannot_add_tools_or_lower_risk_after_trust(tmp_path: Path) -> None:
    (tmp_path / "p.py").write_text(PLUGIN)
    trust_plugin(tmp_path, "p")
    data = json.loads(manifest_path(tmp_path / "p.py").read_text())
    data["tools"] = {"say_hello": 2}  # it was trusted as DANGEROUS; now it says SAFE
    manifest_path(tmp_path / "p.py").write_text(json.dumps(data))
    assert "lowered its risk" in load_plugins(tmp_path, build_default_registry()).failed[0][1]
    data["tools"] = {"other": 0}
    manifest_path(tmp_path / "p.py").write_text(json.dumps(data))
    assert "wasn't in what you trusted" in load_plugins(tmp_path, build_default_registry()).failed[0][1]


def test_critical_plugin_tools_are_refused(tmp_path: Path) -> None:
    (tmp_path / "c.py").write_text("from app.tools.base import Tool, NoArgs, ToolResult\n"
                                   "from app.security.permissions import RiskLevel\n"
                                   "TOOLS = [Tool('wipe', 'x', lambda a, c: ToolResult.success('x'), NoArgs, "
                                   "RiskLevel.CRITICAL)]\n")
    with pytest.raises(ValueError):
        trust_plugin(tmp_path, "c")


@pytest.mark.parametrize(("text", "tool", "arguments"), [
    ("show versions of study", "workflow_versions", {"name": "study"}),
    ("restore version 2 of my morning workflow", "restore_workflow", {"name": "morning", "version": 2}),
])
def test_version_shortcuts(text, tool, arguments) -> None:
    call = match_shortcut(text, lambda name: None)
    assert (call.name, call.arguments) == (tool, arguments)
