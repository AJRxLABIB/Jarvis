import json
import time

import pytest

from app.security import manifest_sign as MS


def _manifest():
    return {"name": "hello", "version": "1.0", "sha256": "ab" * 32, "tools": {"hello_tool": 0}, "capabilities": [], "trusted_at": "2026-11-12T00:00:00+00:00"}


def test_sign_verify_and_tamper(tmp_path):
    folder = tmp_path / "plugins"
    folder.mkdir()
    m = _manifest()
    m["sig"] = MS.sign(folder, m)
    assert MS.verify(folder, m) == (True, "") and MS.key_path(folder).is_file()
    assert MS.verify(folder, {**m, "trusted_at": "later", "description": "x"})[0]          # fields outside the signature may change
    for change in ({"sha256": "cd" * 32}, {"tools": {"hello_tool": 0, "extra": 2}}, {"tools": {"hello_tool": 1}}, {"name": "other"}, {"capabilities": ["network"]}):
        ok, why = MS.verify(folder, {**m, **change})
        assert not ok and "signature does not match" in why, change
    assert "not signed" in MS.verify(folder, {k: v for k, v in m.items() if k != "sig"})[1]


def test_another_installation_cannot_verify(tmp_path):
    a, b = tmp_path / "a" / "plugins", tmp_path / "b" / "plugins"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    m = _manifest()
    m["sig"] = MS.sign(a, m)
    assert "missing" in MS.verify(b, m)[1]                               # no key there at all
    MS.sign(b, _manifest())                                              # b now has its own key
    assert "does not match" in MS.verify(b, m)[1]
    MS.key_path(a).write_text("not hex", encoding="utf-8")
    assert "unreadable" in MS.verify(a, m)[1]
    with pytest.raises(PermissionError):
        MS.sign(a, m)


PLUGIN = '''
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolResult


def hi(_, ctx):
    return ToolResult.success("hi")


TOOLS = [Tool("hello_plugin_tool", "Say hi.", hi, NoArgs, RiskLevel.SAFE, "basic")]
'''


def _legacy(folder):
    from app import plugins

    path = folder / "hello.py"
    path.write_text(PLUGIN, encoding="utf-8")
    manifest = {"name": "hello", "version": "1.0", "sha256": plugins.file_sha(path), "tools": {"hello_plugin_tool": 0}, "capabilities": []}
    plugins.manifest_path(path).write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_legacy_manifest_is_adopted_once_then_signatures_are_required(tmp_path):
    from app import plugins
    from app.agent.tool_manager import ToolRegistry

    folder = tmp_path / "plugins"
    folder.mkdir()
    path = _legacy(folder)
    assert plugins.plugin_status(folder)[0]["status"] == "trusted"       # before signing exists on this PC
    reg = ToolRegistry()
    report = plugins.load_plugins(folder, reg)
    assert reg.get("hello_plugin_tool") is not None and not report.failed
    signed = json.loads(plugins.manifest_path(path).read_text())
    assert signed.get("sig") and MS.has_key(folder)
    # from now on an unsigned or edited manifest is refused
    stripped = {k: v for k, v in signed.items() if k != "sig"}
    plugins.manifest_path(path).write_text(json.dumps(stripped), encoding="utf-8")
    assert plugins.plugin_status(folder)[0]["status"] == "changed"
    reg2 = ToolRegistry()
    rep2 = plugins.load_plugins(folder, reg2)
    assert reg2.get("hello_plugin_tool") is None and "not signed" in str(rep2.failed)
    edited = {**signed, "tools": {"hello_plugin_tool": 0, "sneaky": 0}}
    plugins.manifest_path(path).write_text(json.dumps(edited), encoding="utf-8")
    assert "signature does not match" in str(plugins.load_plugins(folder, ToolRegistry()).failed)


def test_file_and_manifest_rewritten_together_is_caught(tmp_path):
    """The attack the bare hash could not stop: change the plugin AND put its new hash into the manifest."""
    from app import plugins
    from app.agent.tool_manager import ToolRegistry

    folder = tmp_path / "plugins"
    folder.mkdir()
    path = _legacy(folder)
    plugins.load_plugins(folder, ToolRegistry())                         # adopts + signs
    path.write_text(PLUGIN + "\n# changed\n", encoding="utf-8")
    m = json.loads(plugins.manifest_path(path).read_text())
    m["sha256"] = plugins.file_sha(path)
    plugins.manifest_path(path).write_text(json.dumps(m), encoding="utf-8")
    reg = ToolRegistry()
    rep = plugins.load_plugins(folder, reg)
    assert reg.get("hello_plugin_tool") is None and "signature does not match" in str(rep.failed)


def test_agent_run_identity_expiry_and_audit():
    from app.agent.tool_manager import ToolExecutor
    from app.core import orchestrator as O
    from app.core.agents import AGENTS

    run = O.new_agent_run(AGENTS["researcher"], now=1000.0)
    assert run["id"].startswith("ar-") and run["agent"] == "researcher" and run["expires"] == 1000.0 + AGENTS["researcher"].max_seconds
    assert O.new_agent_run(AGENTS["researcher"])["id"] != O.new_agent_run(AGENTS["researcher"])["id"]

    class Ctx:
        agent_contract = None
        agent_run = None

    ctx = Ctx()
    with O.under_contract(ctx, AGENTS["monitor"]):
        assert ctx.agent_run["agent"] == "monitor" and ctx.agent_run["expires"] > time.time()
        with O.under_contract(ctx, AGENTS["coder"]):
            assert ctx.agent_run["agent"] == "coder"
        assert ctx.agent_run["agent"] == "monitor"                       # nesting restores the outer run
    assert ctx.agent_run is None and ctx.agent_contract is None

    records = []
    ex = ToolExecutor.__new__(ToolExecutor)
    ex.context = type("C", (), {"agent_run": {"id": "ar-abc", "agent": "coder"}})()
    ex.audit = type("A", (), {"record": lambda self, event, **kw: records.append((event, kw))})()
    call = type("Call", (), {"name": "read_file", "arguments": {"path": "x"}})()
    ex._audit("tool_result", call, details={"ok": True})
    assert records[0][1]["details"]["agent_run"] == "ar-abc" and records[0][1]["details"]["agent"] == "coder"


def test_expired_agent_run_is_refused_in_the_gate():
    import inspect

    from app.agent.tool_manager import ToolExecutor
    from app.core import retry

    src = inspect.getsource(ToolExecutor._gate)
    assert "agent_run_expired" in src and src.index("agent_contract_denied") < src.index("agent_run_expired")
    assert retry.classify("agent_run_expired") == "permission" and retry.classify("lease_denied") == "permission"
