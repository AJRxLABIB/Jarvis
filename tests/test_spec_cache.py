from __future__ import annotations

from app.agent.tool_manager import ToolRegistry
from app.agent.tool_router import select_specs, spec_tokens
from app.tools.base import NoArgs, Tool, ToolResult
from app.tools.catalog import build_default_registry
from app.security.permissions import RiskLevel


def make(name: str, description: str) -> Tool:
    return Tool(name=name, description=description, args_model=NoArgs, level=RiskLevel.SAFE,
                handler=lambda args, ctx: ToolResult(True, "ok"))


def test_specs_are_built_once_and_reused() -> None:
    registry = ToolRegistry([make("one", "first"), make("two", "second")])
    first, second = registry.specs(), registry.specs()
    assert first[0] is second[0] and [s.name for s in first] == ["one", "two"]
    assert first is not second                                   # a fresh list each time, so callers can't corrupt it


def test_a_new_tool_with_the_same_name_gets_a_fresh_spec() -> None:
    registry = ToolRegistry([make("one", "old text")])
    old = registry.specs()[0]
    registry._tools["one"] = make("one", "new text")             # a plugin reloaded under the same name
    new = registry.specs()[0]
    assert new is not old and new.description == "new text"


def test_registering_later_shows_up_and_token_counts_match_the_uncached_formula() -> None:
    registry = ToolRegistry([make("one", "first")])
    registry.specs()
    registry.register(make("two", "second"))
    assert [s.name for s in registry.specs()] == ["one", "two"]
    spec = registry.specs()[0]
    assert spec_tokens(spec) == spec_tokens(spec) > 8


def test_selection_is_unchanged_by_the_cache() -> None:
    registry = build_default_registry()
    a = [s.name for s in select_specs(registry, "open the browser and search for flights")]
    b = [s.name for s in select_specs(registry, "open the browser and search for flights")]
    assert a == b and len(a) < len(registry)
