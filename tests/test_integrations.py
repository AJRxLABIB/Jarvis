from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.config.settings import Settings
from app.core import integrations
from app.core.integrations import Integration, Status
from app.tools.base import NoArgs, ToolContext
from app.tools.diagnostics_tools import integrations_status


def ctx(**settings) -> ToolContext:
    return ToolContext(Settings(**settings))


def row(rows, key):
    return next(r for r in rows if r["key"] == key)


def test_home_assistant_states_never_show_the_token() -> None:
    rows = integrations.evaluate_all(ctx())
    assert row(rows, "home_assistant")["state"] == "not_set_up"
    half = integrations.evaluate_all(ctx(home_assistant_url="http://x:8123"))
    assert row(half, "home_assistant")["state"] == "problem"
    full = integrations.evaluate_all(ctx(home_assistant_url="http://x:8123", home_assistant_token="SECRET-TOKEN-VALUE"))
    assert row(full, "home_assistant")["state"] == "ready" and "SECRET" not in str(full)


def test_a_new_integration_can_be_registered_and_a_crashing_check_is_contained() -> None:
    def boom(_):
        raise RuntimeError("bad")

    integrations.register(Integration("crash_test", "Crash test", "x", "n/a", boom))
    integrations.register(Integration("ok_test", "Fine test", "x", "n/a", lambda c: Status("ready", "fine")))
    try:
        rows = integrations.evaluate_all(ctx())
        assert row(rows, "crash_test")["state"] == "problem" and "could not be checked" in row(rows, "crash_test")["detail"]
        assert row(rows, "ok_test")["state"] == "ready" and row(rows, "ok_test")["setup"] == ""
    finally:
        integrations._REGISTRY.pop("crash_test")
        integrations._REGISTRY.pop("ok_test")


def test_a_failing_model_is_a_problem_and_mcp_reads_its_config(tmp_path: Path) -> None:
    health = SimpleNamespace(summary=lambda: {"qwen3:8b": {"skipped_for": 30}})
    c = ctx()
    c.ai = SimpleNamespace(health=health)
    assert row(integrations.evaluate_all(c), "ollama")["state"] == "problem"
    (tmp_path / "mcp.json").write_text('{"servers": {"files": {"command": "x"}}}')
    c2 = ctx()
    c2.paths = SimpleNamespace(config_dir=tmp_path, data_dir=tmp_path)
    assert row(integrations.evaluate_all(c2), "mcp")["detail"] == "1 server(s) listed"
    c2.executor = SimpleNamespace(mcp_hub=SimpleNamespace(status={"files": "failed: no such program"}))
    assert row(integrations.evaluate_all(c2), "mcp")["state"] == "problem"


def test_the_tool_summarises() -> None:
    result = integrations_status(NoArgs(), ctx())
    assert result.ok and "Home Assistant" in result.message and len(result.data["integrations"]) >= 6
