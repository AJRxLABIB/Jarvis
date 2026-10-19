"""Words and deeds: the AI said "Opening WhatsApp now" but called no tool (nothing opened - the bug in the user's
screenshot). Now the claimed action really runs through the executor, "do it" after an offer does it, and a claim
that can't be carried out gets an honest correction."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import Field

from app.agent import honesty
from app.agent.tool_manager import ToolRegistry
from app.tools.base import Tool, ToolArgs, ToolResult
from tests.test_agent_loop import build, say


class AppArgs(ToolArgs):
    name: str = Field(default="")
    profile: str = ""


def registry(opened: list):
    def open_app(args, ctx):
        opened.append(args.name)
        return ToolResult.success(f"Opened {args.name}.")
    return ToolRegistry([Tool("open_application", "open an app", open_app, AppArgs)])


def chat(conv, *texts):
    async def go():
        out = []
        try:
            for text in texts:
                pieces: list[str] = []
                reply = await conv.send(text, on_delta=pieces.append)
                out.append((reply, "".join(pieces)))
        finally:
            await conv.ai.aclose()
        return out
    return asyncio.run(go())


@pytest.mark.parametrize("reply,action", [
    ("Opening WhatsApp now. Ready when you are, Sir.", "open WhatsApp"),
    ("Got it — WhatsApp is open. You can now chat.", "open WhatsApp"),
    ("I've opened Spotify for you.", "open Spotify"),
    ("The weather in Dhaka is sunny.", None),
    ("I can open Chrome if you like.", None),
])
def test_claims(reply, action):
    assert honesty.claimed_action(reply) == action


def test_offers_and_affirmations():
    assert honesty.offered_action("However, if you want, I can help you open WhatsApp, or show you how.") == "open WhatsApp"
    assert honesty.offered_action("Want me to launch Spotify?") == "open Spotify"
    assert honesty.offered_action("It is 9 PM.") is None
    assert all(honesty.is_affirmation(t) for t in ("Do It", "I am ready", "yes please", "go ahead", "হ্যাঁ", "ঠিক আছে"))
    assert not any(honesty.is_affirmation(t) for t in ("open notepad", "yes open chrome and spotify", "no"))


def test_a_claimed_open_really_runs():
    opened = []
    conv, _, executions = build([say("Opening WhatsApp now. Ready when you are, Sir.")], registry=registry(opened))
    [(reply, streamed)] = chat(conv, "show my whatsapp chats")
    assert opened == ["WhatsApp"] and [e.call.name for e in reply.tools_used] == ["open_application"]
    assert len(executions) == 1 and "Correction" not in reply.text


def test_do_it_after_an_offer_opens_it_without_the_model():
    opened = []
    conv, fake, _ = build([say("I can't read WhatsApp chats, but I can help you open WhatsApp. Just say the word.")],
                          registry=registry(opened))
    (first, _), (second, _) = chat(conv, "check my whatsapp messages", "Do it")
    assert opened == ["WhatsApp"] and second.model == "direct" and second.text == "Opened WhatsApp."


def test_unbacked_claim_that_cant_run_is_corrected_and_yes_then_does_it():
    conv, _, _ = build([say("I've sent the email to Rafi.")], registry=ToolRegistry([]))
    [(reply, streamed)] = chat(conv, "email rafi the report")
    assert "Correction: no action actually ran" in reply.text and "Correction" in streamed


def test_no_correction_when_a_tool_really_ran_or_nothing_was_claimed():
    opened = []
    conv, _, _ = build([{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "open_application", "arguments": {"name": "Chrome"}}}]}, say("Chrome is open.")], registry=registry(opened))
    [(reply, _)] = chat(conv, "please get chrome going")
    assert opened == ["Chrome"] and "Correction" not in reply.text
    conv, _, _ = build([say("Paris is the capital of France.")], registry=registry(opened))
    [(reply, _)] = chat(conv, "capital of france?")
    assert reply.text == "Paris is the capital of France." and opened == ["Chrome"]


def test_present_app_puts_the_new_window_in_front(monkeypatch):
    import sys
    from types import SimpleNamespace

    from app.tools import applications, winapi

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(winapi, "is_windows", lambda: True)
    state = {"front": 0, "calls": 0}
    window = SimpleNamespace(hwnd=42, cloaked=False)

    def windows(name, ctx):
        state["calls"] += 1
        return name, ([window] if state["calls"] >= 3 else []), set()     # the window appears after a moment

    monkeypatch.setattr(applications, "_target_windows", windows)
    monkeypatch.setattr(winapi, "force_foreground", lambda hwnd: state.update(front=hwnd) or True)
    monkeypatch.setattr(winapi, "foreground_window", lambda: state["front"])
    monkeypatch.setattr(applications.time, "sleep", lambda s: None)
    assert applications.present_app("WhatsApp", None) is True and state["front"] == 42
