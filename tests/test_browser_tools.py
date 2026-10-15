from __future__ import annotations

import asyncio

import httpx
import pytest

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.browser import search as search_module
from app.browser.playwright_manager import Element, Snapshot, check_url
from app.browser.search import DuckDuckGoSearch
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.security.permissions import PermissionEngine, RiskLevel
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from tests.test_tools import ScriptedConfirmer

DDG_HTML = """
<div class="result results_links result--ad"><a class="result__a" href="https://ads.example/x">Sponsored</a></div>
<div class="result results_links"><h2><a class="result__a"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Ftutorial%2F&rut=abc">The Python <b>Tutorial</b></a></h2>
  <a class="result__snippet" href="#">Python is an easy to learn, powerful   programming language.</a></div>
<div class="result results_links"><a class="result__a" href="https://realpython.com/">Real Python</a>
  <div class="result__snippet">Tutorials for developers</div></div>
<div class="result"><a class="result__a" href="javascript:alert(1)">Evil</a></div>
"""


def test_duckduckgo_parsing() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=DDG_HTML))
    results = asyncio.run(DuckDuckGoSearch(transport).search("python tutorial", 5))
    assert [r.title for r in results] == ["The Python Tutorial", "Real Python"]  # ad and javascript: dropped
    assert results[0].url == "https://docs.python.org/3/tutorial/"
    assert results[0].snippet == "Python is an easy to learn, powerful programming language."


def test_search_errors_are_friendly() -> None:
    def boom(request):
        raise httpx.ConnectError("no network")

    with pytest.raises(ToolError, match="internet connection"):
        asyncio.run(DuckDuckGoSearch(httpx.MockTransport(boom)).search("x"))


# ------------------------------------------------------------- fake session


class FakeSession:
    def __init__(self, elements: list[Element]) -> None:
        self.elements = {e.n: e for e in elements}
        self.elements_url = "https://example.com/form"
        self.running = True
        self.clicked: list[int] = []
        self.typed: list[tuple[int, str, bool]] = []

    def element(self, n: int) -> Element:
        if n not in self.elements:
            raise ToolError("no such element", "unknown_element")
        return self.elements[n]

    def _snap(self) -> Snapshot:
        return Snapshot("Example", self.elements_url, "page text", list(self.elements.values()))

    async def click(self, n: int) -> Snapshot:
        self.clicked.append(n)
        return self._snap()

    async def type_text(self, n: int, text: str, submit: bool) -> Snapshot:
        self.typed.append((n, text, submit))
        return self._snap()

    async def snapshot(self) -> Snapshot:
        return self._snap()

    async def close(self) -> None:
        self.running = False


ELEMENTS = [
    Element(1, "a", "", "", "Documentation", href="https://example.com/docs"),
    Element(2, "button", "submit", "", "Send message", in_form=True),
    Element(3, "input", "search", "", "Search", field="q"),
    Element(4, "textarea", "", "", "Your message", field="message", in_form=True),
    Element(5, "input", "password", "", "Password", field="password", in_form=True),
    Element(6, "a", "", "", "Download installer", href="https://example.com/setup.exe"),
    Element(7, "button", "button", "", "Buy now"),
    Element(8, "input", "text", "", "Card number", field="cc-number", autocomplete="cc-number"),
    Element(9, "a", "", "", "Delete my account", href="https://example.com/account/delete"),
    Element(10, "a", "", "", "Next page", href="https://example.com/2"),
]


def run(tool: str, confirm: bool, session: FakeSession | None = None, **arguments):
    ctx = ToolContext(Settings())
    ctx.browser = session or FakeSession(ELEMENTS)
    confirmer = ScriptedConfirmer(confirm)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer)
    return asyncio.run(ex.execute(ToolCall(tool, arguments))), confirmer, ctx.browser


def test_plain_link_clicks_without_asking() -> None:
    execution, confirmer, session = run("browser_click", False, element=1)
    assert execution.result.ok and session.clicked == [1] and not confirmer.requests
    execution, _, session = run("browser_click", False, text="next page")
    assert execution.result.ok and session.clicked == [10]


@pytest.mark.parametrize("target", [{"element": 2}, {"text": "Buy now"}, {"element": 9}])
def test_side_effect_buttons_always_ask(target: dict) -> None:
    execution, confirmer, session = run("browser_click", False, **target)
    assert execution.result.error == "declined" and session.clicked == []
    assert confirmer.requests[0].level == RiskLevel.DANGEROUS and "example.com" in confirmer.requests[0].summary


def test_downloads_are_blocked() -> None:
    execution, confirmer, session = run("browser_click", True, element=6)
    assert execution.result.error == "download_blocked" and session.clicked == [] and not confirmer.requests


def test_clicking_needs_a_read_page_and_known_elements() -> None:
    empty = FakeSession([])
    assert run("browser_click", True, empty, element=1)[0].result.error == "no_snapshot"
    assert run("browser_click", True, element=99)[0].result.error == "unknown_element"
    assert run("browser_click", True)[0].result.error == "invalid_arguments"


def test_search_box_typing_and_submit_is_safe() -> None:
    execution, confirmer, session = run("browser_type", False, field="Search", text="python tutorial", submit=True)
    assert execution.result.ok and session.typed == [(3, "python tutorial", True)] and not confirmer.requests


def test_submitting_a_message_asks_first() -> None:
    execution, confirmer, session = run("browser_type", False, element=4, text="Hello!", submit=True)
    assert execution.result.error == "declined" and session.typed == []
    assert '"Hello!"' in confirmer.requests[0].summary
    execution, confirmer, session = run("browser_type", False, element=4, text="Draft only")
    assert execution.result.ok and session.typed == [(4, "Draft only", False)]  # typing without sending is fine


@pytest.mark.parametrize("target", [{"element": 5}, {"element": 8}, {"field": "Password"}])
def test_passwords_and_card_numbers_are_never_typed(target: dict) -> None:
    execution, confirmer, session = run("browser_type", True, text="hunter2", **target)
    assert execution.result.error == "sensitive" and session.typed == [] and not confirmer.requests


def test_secret_looking_text_is_never_typed_or_searched() -> None:
    execution, _, session = run("browser_type", True, element=3, text="api_key=sk-proj-abcdefghijklmnopqrstu")
    assert execution.result.error == "sensitive" and session.typed == []
    ctx = ToolContext(Settings())
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx)
    assert asyncio.run(ex.execute(ToolCall("web_search", {"query": "my password is hunter2"}))).result.error == "sensitive"


def test_disabled_and_offline() -> None:
    ctx = ToolContext(Settings(browser_enabled=False))
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx)
    assert asyncio.run(ex.execute(ToolCall("browser_open", {"url": "example.com"}))).result.error == "disabled"
    ctx = ToolContext(Settings(offline_mode=True))
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx)
    assert asyncio.run(ex.execute(ToolCall("web_search", {"query": "news"}))).result.error == "offline"


def test_web_search_tool(monkeypatch) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=DDG_HTML))
    monkeypatch.setitem(search_module.PROVIDERS, "duckduckgo", lambda: DuckDuckGoSearch(transport))
    execution, _, _ = run("web_search", False, query="python tutorial", limit=50)
    result = execution.result
    assert result.ok and len(result.data["results"]) == 2 and "untrusted" in result.data["note"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///C:/Windows/win.ini", "chrome://settings", "ftp://x.org"])
def test_only_http_urls(url: str) -> None:
    with pytest.raises(ToolError):
        check_url(url)


def test_urls_get_https() -> None:
    assert check_url("github.com") == "https://github.com"
    assert check_url("http://localhost:3000") == "http://localhost:3000"


def test_label_written_into_the_number_field() -> None:
    """browser_click(element='Tutorial') and element='[10]' from small models."""
    execution, _, session = run("browser_click", False, element="Next page")
    assert execution.result.ok and session.clicked == [10]
    execution, _, session = run("browser_click", False, element="[1]")
    assert execution.result.ok and session.clicked == [1]
    execution, _, session = run("browser_type", False, element="Search", text="pathlib", submit=True)
    assert execution.result.ok and session.typed == [(3, "pathlib", True)]


def test_click_without_open_page_explains_what_to_do() -> None:
    result = run("browser_click", True, FakeSession([]), text="Tutorial")[0].result
    assert result.error == "no_snapshot" and "browser_open" in result.message


def test_search_retries_once() -> None:
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("first request fails")
        return httpx.Response(200, text=DDG_HTML)

    results = asyncio.run(DuckDuckGoSearch(httpx.MockTransport(flaky)).search("python"))
    assert calls["n"] == 2 and len(results) == 2


def test_reading_requests_get_browser_tools() -> None:
    from app.agent.tool_router import select_specs

    names = {s.name for s in select_specs(build_default_registry(), "Open docs.python.org and tell me what's there")}
    assert {"browser_open", "browser_read"} <= names
