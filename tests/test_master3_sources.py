"""Answers say where their facts came from: tools (checked or not), matching saved memories, or only the AI."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent.conversation import answer_sources, sources_line


def _execution(name, ok=True, verified=None):
    data = {} if verified is None else {"verified": verified}
    return SimpleNamespace(call=SimpleNamespace(name=name), result=SimpleNamespace(ok=ok, data=data))


def test_sources_are_built_from_real_results() -> None:
    sources = answer_sources([_execution("open_application", verified=True), _execution("get_weather"),
                              _execution("delete_item", ok=False), _execution("set_volume", verified=False)],
                             [(3, "my city is Dhaka", "it matches words in your request"),
                              (4, "likes tea", "you marked it important")], "jarvis")
    assert [t["verified"] for t in sources["tools"]] == [True, None, None, False]
    assert sources["memories"] == [{"id": 3, "content": "my city is Dhaka"}]       # only matching ones count
    line = sources_line(sources)
    assert "open_application (worked, checked)" in line and "get_weather (worked)" in line
    assert "delete_item (failed)" in line and "set_volume (not confirmed)" in line and "1 saved memory" in line
    assert sources_line(answer_sources([], [], "jarvis")) == ""                     # pure chat: no clutter


def test_answer_sources_tool_is_honest_about_model_only_answers() -> None:
    from app.tools.memory import NoArgs, answer_sources as tool

    ctx = SimpleNamespace(conversation=SimpleNamespace(last_sources=answer_sources([], [], "qwen3")))
    assert "only from the AI model (qwen3)" in tool(NoArgs(), ctx).message
    ctx.conversation.last_sources = answer_sources([_execution("get_weather", verified=True)], [], "qwen3")
    message = tool(NoArgs(), ctx).message
    assert "get_weather worked and I checked the result" in message and "not checked" in message
    assert "no record" in tool(NoArgs(), SimpleNamespace(conversation=None)).message
