from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.browser import pages
from app.tools.base import ToolError
from app.utils import updates


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(pages, "TRANSPORT", None)
    monkeypatch.setattr(pages, "RESOLVE", None)


# ------------------------------------------------------------------ address classification


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.1", "169.254.169.254", "0.0.0.0", "100.64.0.1", "224.0.0.1",
    "::1", "fe80::1", "fc00::1", "::", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
    "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::1", "2002:a9fe:a9fe::1", "not-an-ip", "fe80::1%eth0",
])
def test_non_public_addresses_are_refused(address: str) -> None:
    assert pages.is_public_address(address) is False


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946", "::ffff:8.8.8.8"])
def test_public_addresses_pass(address: str) -> None:
    assert pages.is_public_address(address) is True


@pytest.mark.parametrize("url", [
    "ftp://example.com/x", "file:///etc/passwd", "gopher://example.com", "javascript:alert(1)", "http://", "//example.com",
    "http://user:pass@example.com/", "http://example.com:22/", "http://example.com:6379/", "http://example.com:99999/",
    "http://[::1", "http://exa mple.com", "",
])
def test_malformed_or_unsafe_urls_are_refused(url: str, monkeypatch) -> None:
    monkeypatch.setattr(pages, "RESOLVE", lambda host: ["93.184.216.34"])
    with pytest.raises(ToolError):
        pages.check_public(url)


@pytest.mark.parametrize("url", ["http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/", "http://127.1/",
                                 "http://[::ffff:7f00:1]/", "http://localhost/", "http://169.254.169.254/latest/meta-data/"])
def test_encoded_and_local_hosts_are_refused(url: str, monkeypatch) -> None:
    # the resolver is what turns "2130706433" / "0x7f000001" / "127.1" into 127.0.0.1; it must never reach a request
    table = {"2130706433": "127.0.0.1", "0x7f000001": "127.0.0.1", "0177.0.0.1": "127.0.0.1", "127.1": "127.0.0.1",
             "::ffff:7f00:1": "::ffff:127.0.0.1", "localhost": "127.0.0.1", "169.254.169.254": "169.254.169.254"}
    monkeypatch.setattr(pages, "RESOLVE", lambda host: [table[host]])
    with pytest.raises(ToolError) as err:
        pages.check_public(url)
    assert err.value.code == "private_address"


def test_a_host_with_one_private_answer_among_public_ones_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(pages, "RESOLVE", lambda host: ["93.184.216.34", "10.0.0.5"])
    with pytest.raises(ToolError):
        pages.check_public("https://example.com/")


def test_no_answer_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(pages, "RESOLVE", lambda host: [])
    with pytest.raises(ToolError):
        pages.check_public("https://example.com/")


# --------------------------------------------------------------------------- pinning


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="hello")


def test_connection_goes_to_the_validated_ip_but_keeps_name_and_sni(monkeypatch) -> None:
    monkeypatch.setattr(pages, "RESOLVE", lambda host: ["93.184.216.34"])
    inner = Recorder()
    transport = pages.PinnedTransport(inner)
    response = asyncio.run(transport.handle_async_request(httpx.Request("GET", "https://example.com/a?b=1")))
    sent = inner.requests[0]
    assert response.status_code == 200
    assert sent.url.host == "93.184.216.34" and sent.url.path == "/a" and sent.url.query == b"b=1"
    assert sent.headers["host"] == "example.com"
    assert sent.extensions["sni_hostname"] == "example.com"      # the certificate is checked against the real name


def test_dns_rebinding_cannot_change_the_connection_target(monkeypatch) -> None:
    answers = iter([["93.184.216.34"], ["127.0.0.1"], ["127.0.0.1"]])   # public at first, private afterwards
    monkeypatch.setattr(pages, "RESOLVE", lambda host: next(answers))
    inner = Recorder()
    transport = pages.PinnedTransport(inner)
    asyncio.run(transport.handle_async_request(httpx.Request("GET", "https://rebind.example/")))
    assert inner.requests[0].url.host == "93.184.216.34"        # it connected to what it validated, never re-resolved
    with pytest.raises(ToolError):                              # a later request that resolves privately is refused
        asyncio.run(transport.handle_async_request(httpx.Request("GET", "https://rebind.example/")))
    assert len(inner.requests) == 1


def test_private_literal_ip_is_refused_by_the_transport(monkeypatch) -> None:
    inner = Recorder()
    transport = pages.PinnedTransport(inner)
    for url in ("http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/", "http://[::ffff:10.0.0.1]/"):
        with pytest.raises(ToolError):
            asyncio.run(transport.handle_async_request(httpx.Request("GET", url)))
    assert inner.requests == []


# ----------------------------------------------------------------------- fetch / redirects


def serve(monkeypatch, handler, resolve=None):
    monkeypatch.setattr(pages, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(pages, "RESOLVE", resolve or (lambda host: ["93.184.216.34"]))


def test_redirect_to_a_private_address_is_refused_at_the_hop(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "good.example":
            return httpx.Response(302, headers={"location": "http://internal.example/admin"})
        pytest.fail("the private hop must never be requested")

    serve(monkeypatch, handler, lambda host: ["10.0.0.7"] if host == "internal.example" else ["93.184.216.34"])
    with pytest.raises(ToolError) as err:
        asyncio.run(pages.fetch("https://good.example/"))
    assert err.value.code == "private_address"


def test_redirect_to_a_bad_scheme_or_port_is_refused(monkeypatch) -> None:
    for location in ("file:///etc/passwd", "http://good.example:22/"):
        serve(monkeypatch, lambda request, loc=location: httpx.Response(302, headers={"location": loc}))
        with pytest.raises(ToolError):
            asyncio.run(pages.fetch("https://good.example/"))


def test_redirect_loop_is_bounded(monkeypatch) -> None:
    serve(monkeypatch, lambda request: httpx.Response(302, headers={"location": "https://good.example/next"}))
    with pytest.raises(ToolError) as err:
        asyncio.run(pages.fetch("https://good.example/"))
    assert err.value.code == "http_error"


def test_oversized_and_wrong_type_responses(monkeypatch) -> None:
    serve(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 5_000_000))
    _, _, text = asyncio.run(pages.fetch("https://good.example/"))
    assert len(text) <= pages.MAX_BYTES + 100_000           # read at most about MAX_BYTES, never the whole body
    serve(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"MZ"))
    with pytest.raises(ToolError) as err:
        asyncio.run(pages.fetch("https://good.example/"))
    assert err.value.code == "not_html"


def test_a_server_that_drips_forever_is_cut_off(monkeypatch) -> None:
    monkeypatch.setattr(pages, "TOTAL_SECONDS", 0.2)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, text="late")

    serve(monkeypatch, slow)
    with pytest.raises(ToolError) as err:
        asyncio.run(pages.fetch("https://good.example/"))
    assert err.value.code == "timeout"


# ----------------------------------------------------------------------------- updates


def manifest(url: str = "https://example.com/JarvisSetup-9.0.0.exe") -> dict:
    return {"version": "99.0.0", "url": url, "sha256": "a" * 64, "notes": ""}


def test_manifest_redirect_to_http_is_refused(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://evil.example/update.json"})
        return httpx.Response(200, json=manifest())

    monkeypatch.setattr(updates, "TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(updates.UpdateError, match="unencrypted"):
        updates.check("https://example.com/update.json")


def test_oversized_manifest_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(updates, "TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(200, content=b'{"x": "' + b"a" * 400_000 + b'"}')))
    with pytest.raises(updates.UpdateError, match="large"):
        updates.check("https://example.com/update.json")


def test_http_manifest_is_refused_up_front() -> None:
    with pytest.raises(updates.UpdateError):
        updates.check("http://example.com/update.json")


def test_download_redirect_to_http_is_refused_and_leaves_no_file(monkeypatch, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://mirror.example/setup.exe"})
        return httpx.Response(200, content=b"MZ")

    monkeypatch.setattr(updates, "TRANSPORT", httpx.MockTransport(handler))
    info = updates.UpdateInfo("99.0.0", "https://example.com/JarvisSetup.exe", "a" * 64, "")
    with pytest.raises(updates.UpdateError, match="unencrypted"):
        updates.download(info, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_size_cap_and_cleanup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(updates, "MAX_INSTALLER_BYTES", 1000)
    monkeypatch.setattr(updates, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000)))
    info = updates.UpdateInfo("99.0.0", "https://example.com/JarvisSetup.exe", "a" * 64, "")
    with pytest.raises(updates.UpdateError, match="larger"):
        updates.download(info, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("url,expected", [
    ("https://x.example/dl/JarvisSetup-2.exe?token=1", "JarvisSetup-2.exe"),
    ("https://x.example/..%2f..%2fevil.exe", "evil.exe"),
    ("https://x.example/setup", "JarvisSetup-99.0.0.exe"),
    ("https://x.example/run.bat", "JarvisSetup-99.0.0.exe"),
    ("https://x.example/a b$c.exe", "a_b_c.exe"),
])
def test_installer_file_name_is_plain(url: str, expected: str) -> None:
    name = updates.safe_installer_name(updates.UpdateInfo("99.0.0", url, "a" * 64, ""))
    assert name == expected or name.endswith(expected.split("..")[-1])
    assert "/" not in name and "\\" not in name and not name.startswith(".")
