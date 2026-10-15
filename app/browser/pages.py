"""Reading public web pages as plain text (for research and video summaries), without the browser.

* http/https only, public addresses only: a host that resolves to a private, loopback, link-local or reserved
  address is refused (a page or a model must not be able to make Jarvis read your router or local services).
  Redirects are followed by hand so every hop is checked.
* At most MAX_BYTES are read; only HTML/text is accepted.
* What comes back is untrusted data: callers mark it so and never follow instructions in it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from app import __version__
from app.tools.base import ToolError

TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests replace this
RESOLVE = None  # tests replace this: host -> list of IP strings
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 5
_UA = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) JarvisAssistant/{__version__}"
_SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "template", "aside"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre", "td"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, readable text) of an HTML page."""
    parser = _Text()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    lines = [line.strip() for line in text.split("\n")]
    return " ".join(parser.title.split()), "\n".join(line for line in lines if len(line) > 1 or not line).strip()


def _addresses(host: str) -> list[str]:
    if RESOLVE is not None:
        return list(RESOLVE(host))
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def check_public(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("Only http and https pages can be read.", "invalid_url")
    host = parsed.hostname
    try:
        addresses = _addresses(host)
    except OSError as exc:
        raise ToolError(f"{host} couldn't be found.", "network") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise ToolError(f"{host} is a private or local address; I only read public web pages.", "private_address")
    return url


async def fetch(url: str, timeout: float = 15.0) -> tuple[str, str, str]:
    """(final url, title, text). Raises ToolError with a plain reason."""
    current = url
    async with httpx.AsyncClient(transport=TRANSPORT, timeout=timeout, follow_redirects=False,
                                 headers={"User-Agent": _UA, "Accept": "text/html,text/plain"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await asyncio.to_thread(check_public, current)
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                        current = urljoin(current, response.headers["location"])
                        continue
                    if response.status_code >= 400:
                        raise ToolError(f"The page answered with an error ({response.status_code}).", "http_error")
                    kind = response.headers.get("content-type", "text/html").lower()
                    if not any(t in kind for t in ("html", "text/plain", "xml")):
                        raise ToolError(f"That isn't a web page ({kind.split(';')[0]}).", "not_html")
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    text = body[:MAX_BYTES].decode(response.encoding or "utf-8", errors="replace")
            except httpx.TimeoutException as exc:
                raise ToolError("The page took too long to load.", "timeout") from exc
            except httpx.HTTPError as exc:
                raise ToolError("The page couldn't be loaded.", "network") from exc
            if "html" in kind or "xml" in kind:
                title, readable = html_to_text(text)
                return current, title, readable
            return current, "", text
    raise ToolError("Too many redirects.", "http_error")
