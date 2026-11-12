"""Local read-only MCP server: protocol core + hostile HTTP cases on loopback."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from app.agent.tool_manager import ToolRegistry
from app.mcp import server as S
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolResult


def _tool(name, level=RiskLevel.SAFE, **kw):
    return Tool(name, f"does {name}", lambda a, c: ToolResult.success(f"{name} ran", secret="sk-ABCDEF1234567890abcdef1234567890"), NoArgs, level, "general", **kw)


def _registry():
    r = ToolRegistry()
    for t in (_tool("sensors"), _tool("shutdown_pc", RiskLevel.DANGEROUS), _tool("learning", risk=lambda a, c: RiskLevel.DANGEROUS), _tool("web", needs_network=True),
              _tool("asks", confirmation=lambda a, c: "sure?")):
        r.register(t)
    return r


class FakeExec:
    def __init__(self): self.calls = []
    def __call__(self, call):
        self.calls.append(call.name)
        class E: result = ToolResult.success(f"{call.name} ok", token="sk-ABCDEF1234567890abcdef1234567890")
        return E()


def _msg(method, mid=1, **params):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}


def test_only_safe_whitelisted_tools_are_exposed():
    r = _registry()
    assert set(S.exposable(r, ["sensors", "shutdown_pc", "learning", "web", "asks", "ghost"])) == {"sensors"}
    g = S.Gateway(r, FakeExec(), ["sensors", "shutdown_pc", "learning", "web", "asks"])
    listed = g.handle(_msg("tools/list"))["result"]["tools"]
    assert [t["name"] for t in listed] == ["sensors"]


def test_call_rules():
    ex = FakeExec(); g = S.Gateway(_registry(), ex, ["sensors", "shutdown_pc"])
    ok = g.handle(_msg("tools/call", name="sensors", arguments={}))["result"]
    assert ok["isError"] is False and "sk-ABCDEF" not in ok["content"][0]["text"]            # secrets redacted
    for name in ("shutdown_pc", "ghost", 5):
        assert g.handle(_msg("tools/call", name=name))["error"]["code"] == -32602
    assert ex.calls == ["sensors"]                                                          # nothing else ever reached the executor
    assert g.handle(_msg("resources/read"))["error"]["code"] == -32601
    assert g.handle({"jsonrpc": "1.0"})["error"]["code"] == -32600 and g.handle("x")["error"]["code"] == -32600
    assert g.handle(_msg("tools/call", name="sensors", arguments="no"))["error"]["code"] == -32602
    assert g.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_rate_limit():
    t = [0.0]; g = S.Gateway(_registry(), FakeExec(), ["sensors"], clock=lambda: t[0])
    codes = [g.handle(_msg("tools/call", name="sensors", mid=i)).get("error", {}).get("code") for i in range(S.RATE_PER_MINUTE + 3)]
    assert codes[:S.RATE_PER_MINUTE] == [None] * S.RATE_PER_MINUTE and codes[-1] == -32000
    t[0] = 61.0
    assert "result" in g.handle(_msg("tools/call", name="sensors"))


def test_token_is_stored_hashed_and_replaceable(tmp_path):
    tok = S.create_token(tmp_path)
    stored = S.token_hash(tmp_path)
    assert tok not in (tmp_path / S.TOKEN_FILE).read_text() and S.token_ok(f"Bearer {tok}", stored)
    assert not S.token_ok("Bearer nope", stored) and not S.token_ok(f"Basic {tok}", stored) and not S.token_ok(f"Bearer {tok}", "")
    tok2 = S.create_token(tmp_path)
    assert not S.token_ok(f"Bearer {tok}", S.token_hash(tmp_path)) and S.token_ok(f"Bearer {tok2}", S.token_hash(tmp_path))
    assert S.revoke_token(tmp_path) and S.token_hash(tmp_path) == "" and not S.revoke_token(tmp_path)


def test_host_and_origin_checks():
    assert S.host_ok("127.0.0.1:8766") and S.host_ok("localhost") and S.host_ok("[::1]:9")
    assert not S.host_ok("evil.example.com") and not S.host_ok("192.168.1.5:8766") and not S.host_ok("")
    assert S.origin_ok("") and S.origin_ok("http://localhost:3000") and not S.origin_ok("https://evil.example")


@pytest.fixture()
def live(tmp_path):

    class AsyncExec:
        async def execute(self, call):
            return FakeExec()(call)
    srv = S.McpServer(_registry(), AsyncExec(), tmp_path, port=0, names=["sensors", "shutdown_pc"])
    token = S.create_token(tmp_path)
    port = srv.start()
    yield srv, port, token
    srv.stop()


def _post(port, body, token=None, headers=None, path="/mcp"):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body if isinstance(body, bytes) else json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, None


def test_http_end_to_end_and_hostile_requests(live):
    srv, port, token = live
    assert srv._http.server_address[0] == "127.0.0.1"
    code, body = _post(port, _msg("initialize"), token)
    assert code == 200 and body["result"]["serverInfo"]["name"] == "jarvis-readonly"
    code, body = _post(port, _msg("tools/call", name="sensors"), token)
    assert code == 200 and "sensors ok" in body["result"]["content"][0]["text"]
    assert _post(port, _msg("tools/call", name="shutdown_pc"), token)[1]["error"]["code"] == -32602
    assert _post(port, _msg("tools/list"))[0] == 401                                           # no token
    assert _post(port, _msg("tools/list"), "wrong")[0] == 401
    assert _post(port, _msg("tools/list"), token, {"Host": "evil.example.com"})[0] == 403      # DNS rebinding
    assert _post(port, _msg("tools/list"), token, {"Origin": "https://evil.example"})[0] == 403  # a web page cannot call it
    assert _post(port, _msg("tools/list"), token, path="/other")[0] == 404
    assert _post(port, b"{not json", token)[0] == 400
    assert _post(port, b"x" * (S.MAX_BODY + 10), token)[0] == 413
    assert _post(port, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token)[0] == 202
    S.revoke_token(srv.data_dir)
    assert _post(port, _msg("tools/list"), token)[0] == 401                                    # revoking takes effect at once
