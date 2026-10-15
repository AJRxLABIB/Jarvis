"""Web search providers. Results are real (fetched now) and untrusted."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app import __version__
from app.tools.base import ToolError

_USER_AGENT = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) JarvisAssistant/{__version__}"


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class SearchProvider(ABC):
    name = "base"

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[SearchResult]: ...


class _DuckDuckGoParser(HTMLParser):
    """Parses html.duckduckgo.com result pages (no JavaScript needed)."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._field: str | None = None
        self._skip_ad = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div" and "result" in classes:
            self._skip_ad = "result--ad" in classes
            if not self._skip_ad:
                self.results.append({"title": "", "url": "", "snippet": ""})
        if self._skip_ad or not self.results:
            return
        if tag == "a" and "result__a" in classes:
            self._field = "title"
            self.results[-1]["url"] = _real_url(a.get("href") or "")
        elif tag in ("a", "div") and "result__snippet" in classes:
            self._field = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("a", "div"):
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._field and self.results and not self._skip_ad:
            self.results[-1][self._field] += data


def _real_url(href: str) -> str:
    """DuckDuckGo wraps links as //duckduckgo.com/l/?uddg=<real url>."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)
    return href


class DuckDuckGoSearch(SearchProvider):
    name = "duckduckgo"
    URL = "https://html.duckduckgo.com/html/"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 15.0) -> None:
        self._transport = transport
        self._timeout = timeout

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        import asyncio

        response = None
        for attempt in range(2):  # one retry: first requests after waking up / DNS hiccups often fail
            try:
                async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout, follow_redirects=True,
                                             headers={"User-Agent": _USER_AGENT}) as client:
                    response = await client.post(self.URL, data={"q": query, "kl": "wt-wt"})
                if response.status_code < 500:
                    break
            except httpx.TimeoutException as exc:
                if attempt:
                    raise ToolError("The search took too long. Check the internet connection.", "timeout") from exc
            except httpx.HTTPError as exc:
                if attempt:
                    raise ToolError("Couldn't reach the search service. Check the internet connection.", "network") from exc
            await asyncio.sleep(1.0)
        assert response is not None
        if response.status_code >= 400:
            raise ToolError(f"The search service returned an error ({response.status_code}).", "search_failed")
        parser = _DuckDuckGoParser()
        parser.feed(response.text)
        results = []
        for item in parser.results:
            title, url = " ".join(item["title"].split()), item["url"]
            if not title or not url.startswith(("http://", "https://")):
                continue
            results.append(SearchResult(title, url, " ".join(item["snippet"].split())[:300]))
            if len(results) >= limit:
                break
        return results


PROVIDERS = {"duckduckgo": DuckDuckGoSearch}


def create_search_provider(name: str) -> SearchProvider:
    return PROVIDERS[name]()
