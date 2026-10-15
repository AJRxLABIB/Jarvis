from __future__ import annotations

import asyncio

from app.ai.base import ToolCall
from tests.test_v3230 import make


def run(call: ToolCall):
    ex, log = make()
    return asyncio.run(ex.execute(call)), log


def test_unknown_tool_gets_a_did_you_mean() -> None:
    execution, log = run(ToolCall("web_serch", {"name": "x"}))
    assert execution.result.error == "unknown_tool" and "web_search" in execution.result.message and log == []


def test_nonsense_tool_lists_what_exists() -> None:
    execution, _ = run(ToolCall("zzzzzz", {}))
    assert "Available:" in execution.result.message


def test_oversized_arguments_are_refused_before_anything_runs() -> None:
    execution, log = run(ToolCall("web_search", {"name": "x" * 300_000}))
    assert execution.result.error == "invalid_arguments" and "too large" in execution.result.message and log == []


def test_arguments_that_are_not_plain_data_are_refused() -> None:
    class Weird:
        def __repr__(self) -> str:
            raise ValueError("nope")

    execution, log = run(ToolCall("web_search", {"name": Weird()}))
    assert not execution.result.ok and log == []


def test_malformed_arguments_and_wrong_types_are_refused() -> None:
    bad, log = run(ToolCall("web_search", {}, parse_error="not JSON"))
    assert bad.result.error == "invalid_arguments" and log == []
    wrong, log = run(ToolCall("web_search", {"name": {"nested": 1}}))
    assert wrong.result.error == "invalid_arguments" and log == []
