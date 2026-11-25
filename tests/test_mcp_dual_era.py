"""Round 94: MCP dual-era client/server (spec 2026-07-28: per-request _meta, server/discover, UnsupportedProtocolVersionError -32022)."""

import json

import httpx
import pytest

from app.mcp import client as C


class Fake(C._Base):
    """A scripted server: ``answers`` maps a method to a result dict or an McpError to raise."""

    def __init__(self, answers):
        super().__init__(timeout=1)
        self.answers, self.sent, self.notes = answers, [], []

    def request(self, method, params=None, timeout=None):
        params = self._params(params)
        self.sent.append((method, params))
        a = self.answers.get(method)
        if a is None:
            raise C.McpError("method not found", -32601)
        if isinstance(a, Exception):
            raise a
        return a

    def notify(self, method, params=None):
        self.notes.append(method)


def test_modern_server_needs_no_handshake_and_every_request_carries_meta():
    f = Fake({"server/discover": {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "_meta": {C.META_SERVER: {"name": "s"}}},
              "tools/list": {"tools": [{"name": "t"}]}})
    f.initialize()
    assert f.era == "modern" and f.version == "2026-07-28" and f.server_info == {"name": "s"} and f.notes == []
    assert [t["name"] for t in f.list_tools()] == ["t"]
    method, params = f.sent[-1]
    assert params["_meta"][C.META_VERSION] == "2026-07-28" and C.META_CLIENT in params["_meta"] and C.META_CAPS in params["_meta"]
    assert all(m != "initialize" for m, _ in f.sent)


def test_legacy_server_falls_back_to_initialize_without_meta():
    f = Fake({"initialize": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "old"}, "capabilities": {}}, "tools/list": {"tools": []}})
    f.initialize()
    assert f.era == "legacy" and f.notes == ["notifications/initialized"] and f.server_info == {"name": "old"}
    f.list_tools()
    assert "_meta" not in f.sent[-1][1]


def test_unsupported_version_error_retries_with_a_mutual_version_or_explains():
    err = C.McpError("Unsupported protocol version", C.UNSUPPORTED_VERSION, {"supported": ["2026-07-28", "2025-11-25"], "requested": "x"})
    f = Fake({"server/discover": err})
    f.initialize()
    assert f.era == "modern" and f.version == "2026-07-28"
    none = Fake({"server/discover": C.McpError("Unsupported protocol version", C.UNSUPPORTED_VERSION, {"supported": ["2099-01-01"]})})
    with pytest.raises(C.McpError, match="no common MCP version"):
        none.initialize()


def test_modern_server_offering_only_a_legacy_version_uses_the_handshake():
    f = Fake({"server/discover": {"supportedVersions": ["2025-11-25"]}, "initialize": {"protocolVersion": "2025-11-25"}})
    f.initialize()
    assert f.era == "legacy" and f.version == "2025-11-25"
    assert ("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": C.CLIENT_INFO}) in f.sent


def test_probe_can_be_switched_off_and_pick_version():
    f = Fake({"initialize": {"protocolVersion": "2025-06-18"}})
    f.probe = False
    f.initialize()
    assert [m for m, _ in f.sent] == ["initialize"]
    assert C.pick_version(["2025-06-18", "2026-07-28"]) == ("modern", "2026-07-28") and C.pick_version(["1900-01-01"]) is None and C.pick_version(None) is None


def test_http_modern_error_body_on_400_is_understood_and_header_is_sent():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("MCP-Protocol-Version"))
        body = json.loads(request.content)
        if body["method"] == "server/discover":
            return httpx.Response(400, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32022, "message": "Unsupported protocol version",
                                                                                           "data": {"supported": ["2026-07-28"]}}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}})

    h = C.HttpClient("http://x/mcp", transport=httpx.MockTransport(handler))
    h.initialize()
    assert h.era == "modern"
    h.list_tools()
    assert seen[-1] == "2026-07-28"


def test_local_server_is_dual_era():
    from app.mcp.server import Gateway, SUPPORTED

    g = Gateway.__new__(Gateway)          # only the protocol paths that need no registry
    r = Gateway.handle(g, {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": {C.META_VERSION: "2026-07-28"}}})
    assert r["result"]["supportedVersions"] == list(SUPPORTED)
    bad = Gateway.handle(g, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": {C.META_VERSION: "1900-01-01"}}})
    assert bad["error"]["code"] == -32022 and "2026-07-28" in bad["error"]["data"]["supported"]
    old = Gateway.handle(g, {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})
    assert old["result"]["protocolVersion"] == "2025-06-18"


def test_hub_overview_reports_the_negotiated_era():
    from types import SimpleNamespace as NS

    from app.mcp.tools import McpHub

    hub = McpHub.__new__(McpHub)
    hub.status = {"a": "2 tool(s)", "b": "failed: boom"}
    hub.servers = {"a": NS(config={"url": "http://x"}, client=NS(era="modern", version="2026-07-28", capabilities={"tools": {}, "extensions": {"io.modelcontextprotocol/tasks": {}}}))}
    lines = hub.overview()
    assert "modern protocol 2026-07-28" in lines[0] and "not used by Jarvis" in lines[0] and lines[1] == "b: failed: boom"
