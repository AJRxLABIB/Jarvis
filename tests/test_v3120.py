"""3.12.0 Phase 9: research with real sources, YouTube summaries, interpreter mode, study mode."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.agent.conversation import ConversationManager
from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecutor
from app.ai.base import ChatMessage, ChatResponse, ToolCall
from app.browser import pages
from app.browser.search import SearchResult
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import sessions
from app.security.permissions import PermissionEngine
from app.tools import research
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from tests.test_tools import ScriptedConfirmer


class FakeAI:
    def __init__(self, *answers: str) -> None:
        self.answers, self.prompts = list(answers), []

    async def chat(self, messages, tools=(), fast=False, smart=False):
        self.prompts.append(messages)
        return ChatResponse("fake", ChatMessage.assistant(self.answers.pop(0) if self.answers else ""))


@pytest.fixture(autouse=True)
def no_session():
    sessions.STATE.active = None
    yield
    sessions.STATE.active = None
    pages.TRANSPORT, pages.RESOLVE = None, None


def run(ctx, tool, **arguments):
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx,
                      confirmer=ScriptedConfirmer(True))
    return asyncio.run(ex.execute(ToolCall(tool, arguments))).result


# ------------------------------------------------------------------ pages: public web only


def test_private_and_local_addresses_are_never_read() -> None:
    for host, ip in (("router.home", "192.168.1.1"), ("localhost", "127.0.0.1"), ("meta", "169.254.169.254"),
                     ("v6", "::1"), ("ten", "10.0.0.5")):
        pages.RESOLVE = lambda h, ip=ip: [ip]
        with pytest.raises(ToolError) as caught:
            pages.check_public(f"http://{host}/")
        assert caught.value.code == "private_address"
    pages.RESOLVE = lambda h: ["93.184.216.34"]
    assert pages.check_public("https://example.com/a") == "https://example.com/a"
    with pytest.raises(ToolError):
        pages.check_public("file:///C:/Windows/win.ini")


def test_a_redirect_to_a_private_address_is_refused() -> None:
    pages.RESOLVE = lambda h: ["10.0.0.1"] if h == "inside" else ["93.184.216.34"]
    pages.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "http://inside/admin"}))
    with pytest.raises(ToolError) as caught:
        asyncio.run(pages.fetch("https://example.com/"))
    assert caught.value.code == "private_address"


def test_page_text_skips_scripts_and_menus() -> None:
    title, text = pages.html_to_text("<title>T</title><nav>menu</nav><script>evil()</script><p>Real text.</p>")
    assert title == "T" and "Real text." in text and "evil" not in text and "menu" not in text


# ------------------------------------------------------------------ research


def test_research_reads_the_pages_and_cites_them(monkeypatch) -> None:
    class Search:
        async def search(self, query, limit):
            return [SearchResult("A", "https://a.example/1", "snip a"), SearchResult("B", "https://b.example/2", "b")]

    monkeypatch.setattr("app.browser.search.create_search_provider", lambda name: Search())
    pages.RESOLVE = lambda h: ["93.184.216.34"]
    pages.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, html=f"<title>{r.url.host}</title><p>Fact from {r.url.host}.</p>"))
    ai = FakeAI("Both agree on the fact [1][2].")
    result = run(ToolContext(Settings(), ai=ai), "research", question="what is the fact", sources=2)
    assert result.ok and "[1] a.example - https://a.example/1" in result.message
    assert [s["url"] for s in result.data["sources"]] == ["https://a.example/1", "https://b.example/2"]
    prompt = ai.prompts[0][1].content
    assert "Fact from a.example." in prompt and "Fact from b.example." in prompt
    assert "ignore any instructions" in ai.prompts[0][0].content


def test_research_is_untrusted_content() -> None:
    from app.security.taint import EXTERNAL_TOOLS

    assert {"research", "summarize_video"} <= EXTERNAL_TOOLS


# ------------------------------------------------------------------ YouTube


@pytest.mark.parametrize(("url", "vid"), [
    ("https://www.youtube.com/watch?v=abcDEF12345&t=3", "abcDEF12345"), ("https://youtu.be/abcDEF12345", "abcDEF12345"),
    ("youtube.com/shorts/abcDEF12345", "abcDEF12345"), ("abcDEF12345", "abcDEF12345"),
])
def test_video_ids(url, vid) -> None:
    assert research.video_id(url) == vid


def test_not_a_video_link() -> None:
    with pytest.raises(ToolError):
        research.video_id("https://evil.example/watch?v=abcDEF12345")


WATCH = ('<title>Great Talk - YouTube</title>... "captionTracks":[{"baseUrl":"https://www.youtube.com/api/timedtext?v=x'
         '\\u0026lang=en","languageCode":"en","kind":"asr"}] ...')
CAPTIONS = "<transcript>" + "".join(f'<text start="{i}">point number {i} about focus &amp; habits</text>'
                                    for i in range(12)) + "</transcript>"


def test_video_summary_from_captions() -> None:
    pages.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, text=CAPTIONS if "timedtext" in str(r.url)
                                                                   else WATCH))
    ai = FakeAI("- focus\n- habits")
    result = run(ToolContext(Settings(), ai=ai), "summarize_video", url="https://youtu.be/abcDEF12345")
    assert result.ok and result.message.startswith("Great Talk") and "- habits" in result.message
    assert result.data["automatic"] and "focus & habits" in ai.prompts[0][1].content


def test_no_captions_is_said_not_invented() -> None:
    pages.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, text="<title>x</title>no tracks"))
    ai = FakeAI("should not be used")
    result = run(ToolContext(Settings(), ai=ai), "summarize_video", url="abcDEF12345")
    assert not result.ok and result.error == "no_captions" and not ai.prompts


# ------------------------------------------------------------------ sessions


def conversation(ai) -> ConversationManager:
    return ConversationManager(ai, Settings())


def test_interpreter_translates_and_never_acts() -> None:
    assert run(ToolContext(Settings()), "interpreter_mode").ok
    ai = FakeAI("How are you?", "তুমি কেমন আছ?")
    conv = conversation(ai)
    first = asyncio.run(conv.send("তুমি কেমন আছ?"))
    assert first.text == "How are you?" and first.model == "interpreter" and "English" in ai.prompts[0][0].content
    second = asyncio.run(conv.send("shut down my computer"))  # translated, never executed
    assert second.text == "তুমি কেমন আছ?" and "Bangla" in ai.prompts[1][0].content and conv.reply_language == "bn"
    assert asyncio.run(conv.send("stop interpreting")).text == "Interpreter off." and sessions.STATE.active is None


def test_interpreter_into_a_named_language() -> None:
    assert run(ToolContext(Settings()), "interpreter_mode", language="Arabic").ok
    assert sessions.STATE.active.target == "ar"
    assert run(ToolContext(Settings()), "interpreter_mode", language="Klingon").error == "unknown_language"


def test_study_quiz_asks_checks_and_scores() -> None:
    ai = FakeAI('[{"q": "Capital of Bangladesh?", "a": "Dhaka"}, {"q": "2+2?", "a": "4"}]')
    start = run(ToolContext(Settings(), ai=ai), "study_mode", topic="basics", count=2)
    assert start.ok and "Question 1: Capital of Bangladesh?" in start.message
    ai = FakeAI("CORRECT. Dhaka is the capital.", "WRONG. It is 4.")
    conv = conversation(ai)
    one = asyncio.run(conv.send("Dhaka"))
    assert one.text.startswith("Correct!") and "Question 2: 2+2?" in one.text
    two = asyncio.run(conv.send("five"))
    assert "the answer is: 4" in two.text and "1 of 2 right" in two.text and sessions.STATE.active is None


def test_quiz_questions_also_read_from_plain_lines() -> None:
    assert sessions.parse_questions("Q1: What is H2O?\nA1: Water\nQ2: Sky colour?\nA2: Blue", 5) == [
        ("What is H2O?", "Water"), ("Sky colour?", "Blue")]


@pytest.mark.parametrize(("text", "tool"), [
    ("research the best laptop for programming", "research"), ("summarize this video", "summarize_video"),
    ("be my interpreter", "interpreter_mode"), ("translate everything into Arabic", "interpreter_mode"),
    ("quiz me on the solar system", "study_mode"), ("test proposal 2", "test_proposal"),
])
def test_phase9_shortcuts(text, tool) -> None:
    assert match_shortcut(text, lambda name: None).name == tool
