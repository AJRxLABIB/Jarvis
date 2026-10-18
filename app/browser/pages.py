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


ALLOWED_PORTS = (80, 443, 8080, 8443)
TOTAL_SECONDS = 30.0   # the whole fetch, however slowly the server drips data
# 64:ff9b::/96 (NAT64) and 2002::/16 (6to4) embed an IPv4 address that must be judged as that address
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


def _addresses(host: str) -> list[str]:
    if RESOLVE is not None:
        return list(RESOLVE(host))
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def is_public_address(address: str) -> bool:
    """True only for a globally routable unicast address. IPv4 hidden inside an IPv6 address is judged as IPv4."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_address(str(ip.ipv4_mapped))
        if ip in _NAT64:
            return is_public_address(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        if ip in _SIX_TO_FOUR:
            return is_public_address(str(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)))
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_private
                                       or ip.is_reserved or ip.is_unspecified)


def _check_url_shape(url: str) -> tuple[str, int]:
    """Scheme, credentials and port rules; returns (host, port). Malformed addresses raise a plain ToolError."""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ToolError("That isn't a valid web address.", "invalid_url")
    try:
        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ToolError("That isn't a valid web address.", "invalid_url") from exc
    if parsed.hostname and not re.fullmatch(r"[A-Za-z0-9._:\-\[\]%]+", parsed.hostname):
        raise ToolError("That isn't a valid web address.", "invalid_url")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("Only http and https pages can be read.", "invalid_url")
    if parsed.username or parsed.password:
        raise ToolError("Addresses with a user name or password in them are not read.", "invalid_url")
    if port not in ALLOWED_PORTS:
        raise ToolError(f"Port {port} is not allowed; only the normal web ports are read.", "invalid_port")
    return parsed.hostname, port


def check_public(url: str) -> str:
    host, _ = _check_url_shape(url)
    try:
        addresses = _addresses(host)
    except OSError as exc:
        raise ToolError(f"{host} couldn't be found.", "network") from exc
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise ToolError(f"{host} is a private or local address; I only read public web pages.", "private_address")
    return url


class PinnedTransport(httpx.AsyncBaseTransport):
    """Connects to the address it just validated, so a DNS answer that changes between the check and the connection
    (DNS rebinding) cannot send the request somewhere private.

    The host is resolved here ONCE per request, every address must be public, and the connection goes to that IP. The
    original host name is kept for the ``Host`` header and as the TLS server name, so the certificate is still verified
    against the real name. ``inner`` is the transport that really connects (replaceable in tests).
    """

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        try:
            literal = ipaddress.ip_address(host)
            addresses = [str(literal)]
        except ValueError:
            try:
                addresses = await asyncio.to_thread(_addresses, host)
            except OSError as exc:
                raise httpx.ConnectError(f"{host} couldn't be found") from exc
        if not addresses or not all(is_public_address(a) for a in addresses):
            raise ToolError(f"{host} is a private or local address; I only read public web pages.", "private_address")
        ip = addresses[0]
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = host
        headers = httpx.Headers(request.headers)
        headers["Host"] = request.headers.get("Host", host)
        pinned = httpx.Request(request.method, request.url.copy_with(host=ip), headers=headers,
                               stream=request.stream, extensions=extensions)
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def fetch(url: str, timeout: float = 15.0) -> tuple[str, str, str]:
    """(final url, title, text). Raises ToolError with a plain reason."""
    try:
        return await asyncio.wait_for(_fetch(url, timeout), TOTAL_SECONDS)
    except asyncio.TimeoutError as exc:
        raise ToolError("The page took too long to load.", "timeout") from exc


async def _fetch(url: str, timeout: float) -> tuple[str, str, str]:
    current = url
    transport = TRANSPORT if TRANSPORT is not None else PinnedTransport()
    # trust_env=False: no proxy from the environment, which would resolve and connect on its own and skip the pinning
    async with httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False,
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
