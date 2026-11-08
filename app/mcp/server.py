"""A controlled, LOCAL, READ-ONLY MCP server: lets another program on THIS PC (Claude Desktop, an IDE agent) call a short whitelist of Jarvis tools.

Off by default (MCP_SERVER_ENABLED). Safety design - every point is covered by tests/test_mcp_server.py:

* binds to 127.0.0.1 only, and refuses requests whose Host is not localhost / 127.0.0.1 (DNS-rebinding) or that carry a foreign Origin (a web page
  in your browser cannot call it);
* every request needs ``Authorization: Bearer <token>``; only the sha256 of the token is stored (``python run.py --mcp-server-token`` shows it once);
* WHITELIST, not blacklist: only tools named in MCP_SERVER_TOOLS (default: a few status tools) AND registered as SAFE with no argument-dependent risk
  raise are listed or callable - nothing can write, delete, send, shut down or change settings through it;
* each call goes through the normal executor (validation, permissions, taint, rate limit, timeout, audit) as an AI-origin call, never as "the user's
  own command"; its answer is secret-redacted and capped;
* its own rate limit (30 calls / minute), a body cap (64 KB) and a per-call timeout; unknown methods are refused.
Only the tools part of MCP is served (initialize, ping, tools/list, tools/call). Verified with local test clients only.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from app.ai.base import ToolCall
from app.security.permissions import RiskLevel
from app.utils.logging_setup import get_logger, log_event
from app.utils.redaction import redact

_log = get_logger("mcp.server")
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TOOLS = ("sensors", "what_changed", "situations", "system_status", "service_impact", "plugin_check")
MAX_BODY = 64 * 1024
MAX_REPLY = 12_000
RATE_PER_MINUTE = 30
TOKEN_FILE = "mcp_server.token"
HOSTS = {"127.0.0.1", "localhost", "[::1]"}


# ---- token (only a hash is stored) --------------------------------------------------------------------------------------------------------

def create_token(data_dir: Path) -> str:
    """A new token (replaces the old one). Shown to the person once; only its hash is kept."""
    token = "jmcp_" + secrets.token_urlsafe(32)
    path = Path(data_dir) / TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hashlib.sha256(token.encode()).hexdigest(), encoding="utf-8")
    return token


def token_hash(data_dir: Path) -> str:
    try:
        return (Path(data_dir) / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def revoke_token(data_dir: Path) -> bool:
    try:
        (Path(data_dir) / TOKEN_FILE).unlink()
        return True
    except OSError:
        return False


def token_ok(header: str, stored_hash: str) -> bool:
    if not stored_hash or not header.startswith("Bearer "):
        return False
    given = hashlib.sha256(header[7:].strip().encode()).hexdigest()
    return hmac.compare_digest(given, stored_hash)


# ---- the protocol core (no sockets: tests call handle() directly) --------------------------------------------------------------------------

def exposable(registry: Any, names: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """The tools that may be served: named in the whitelist, registered, SAFE, and not able to raise their own risk from arguments."""
    out = {}
    for name in names:
        tool = registry.get(name)
        if tool is not None and tool.level == RiskLevel.SAFE and tool.risk is None and tool.confirmation is None and not tool.needs_network:
            out[name] = tool
    return out


class Gateway:
    def __init__(self, registry: Any, execute: Callable[[ToolCall], Any], names: tuple[str, ...] | list[str], clock: Callable[[], float] = time.monotonic) -> None:
        self.registry, self.execute, self.names, self.clock = registry, execute, tuple(names), clock
        self.calls: deque[float] = deque()

    def _rate_ok(self) -> bool:
        now = self.clock()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        if len(self.calls) >= RATE_PER_MINUTE:
            return False
        self.calls.append(now)
        return True

    def handle(self, message: Any) -> dict | None:
        """One JSON-RPC message -> reply (None for notifications)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(None, -32600, "invalid request")
        mid, method, params = message.get("id"), message["method"], message.get("params") or {}
        if not isinstance(params, dict):
            return _error(mid, -32602, "invalid params")
        if mid is None:                                          # a notification (e.g. notifications/initialized): nothing to answer
            return None
        if method == "initialize":
            return _ok(mid, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "jarvis-readonly", "version": "1"}})
        if method == "ping":
            return _ok(mid, {})
        tools = exposable(self.registry, self.names)
        if method == "tools/list":
            specs = {s.name: s for s in self.registry.specs()}
            return _ok(mid, {"tools": [{"name": n, "description": t.description[:400], "inputSchema": dict(specs[n].parameters) if n in specs else {"type": "object"}}
                                       for n, t in tools.items()]})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if not isinstance(name, str) or name not in tools or not isinstance(args, dict):
                return _error(mid, -32602, "unknown or not allowed tool")
            if not self._rate_ok():
                return _error(mid, -32000, "rate limit: too many calls, slow down")
            try:
                result = self.execute(ToolCall(name, args)).result
            except Exception:
                _log.warning("mcp server call failed", exc_info=True)
                return _ok(mid, {"content": [{"type": "text", "text": "the tool failed"}], "isError": True})
            text = redact(result.to_model_text())[:MAX_REPLY]
            log_event(_log, "mcp_server_call", tool=name, ok=bool(result.ok))
            return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": not result.ok})
        return _error(mid, -32601, "method not supported")


def _ok(mid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


# ---- HTTP transport -------------------------------------------------------------------------------------------------------------------------

def host_ok(host: str) -> bool:
    name = (host or "").strip().lower()
    base = name.rsplit(":", 1)[0] if name.count(":") == 1 or (name.startswith("[") and "]:" in name) else name
    return base in HOSTS or base.rstrip("]") + "]" in HOSTS


def origin_ok(origin: str) -> bool:
    return not origin or origin.split("://", 1)[-1].split(":")[0].split("/")[0] in ("localhost", "127.0.0.1")


class McpServer:
    def __init__(self, registry: Any, executor: Any, data_dir: Path, port: int = 8766, names: tuple[str, ...] | list[str] = DEFAULT_TOOLS, timeout: float = 20.0) -> None:
        self.data_dir, self.port, self.timeout = Path(data_dir), port, timeout
        self._loop = asyncio.new_event_loop()
        self._executor = executor
        self.gateway = Gateway(registry, self._run_call, names)
        self._http: ThreadingHTTPServer | None = None

    def _run_call(self, call: ToolCall):
        future = asyncio.run_coroutine_threadsafe(self._executor.execute(call), self._loop)
        return future.result(self.timeout)

    def start(self) -> int:
        threading.Thread(target=self._loop.run_forever, name="mcp-server-loop", daemon=True).start()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                return

            def _send(self, code: int, body: dict | None = None) -> None:
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                if not host_ok(self.headers.get("Host", "")) or not origin_ok(self.headers.get("Origin", "")):
                    return self._send(403, {"error": "forbidden"})
                if self.path.split("?")[0] != "/mcp":
                    return self._send(404, {"error": "not found"})
                if not token_ok(self.headers.get("Authorization", ""), token_hash(server.data_dir)):
                    return self._send(401, {"error": "a valid token is required"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return self._send(400, {"error": "bad length"})
                if length <= 0 or length > MAX_BODY:
                    return self._send(413, {"error": "body too large"})
                try:
                    message = json.loads(self.rfile.read(length))
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
                reply = server.gateway.handle(message)
                self._send(202 if reply is None else 200, reply)

            def do_GET(self) -> None:
                self._send(405, {"error": "POST only"})

        self._http = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)      # loopback only, never 0.0.0.0
        self.port = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever, name="mcp-server", daemon=True).start()
        log_event(_log, "mcp_server_started", port=self.port, tools=sorted(exposable(self.gateway.registry, self.gateway.names)))
        return self.port

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        self._loop.call_soon_threadsafe(self._loop.stop)
